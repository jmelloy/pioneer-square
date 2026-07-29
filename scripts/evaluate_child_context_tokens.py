#!/usr/bin/env python3
"""Measure whether FOREMAN_CHILD_CONTEXTS is actually saving foreman tokens.

Per docs/foreman-per-task-context.md, when FOREMAN_CHILD_CONTEXTS=1 (the
default) task-triggered foreman turns (task-complete, followup-done,
needs-input, task-error) run against an isolated per-task history instead of
the full guild-wide conversation. Every foreman assistant turn is persisted
to the ``foreman_turns`` table with a ``task_id`` column that tells us which
regime it ran under:

    task_id IS NOT NULL  -> child context   (isolated per-task history)
    task_id IS NULL       -> parent context  (whole-guild shared history)

This script pulls those turns via the ``/debug/query`` endpoint (gated by
DEBUG_TOKEN), parses each turn's ``api_calls_json`` for real per-call token
counts (input/output/cache_read/cache_write — the same numbers billed by the
provider), and reports totals/averages for child vs. parent turns so you can
see whether child contexts are actually cheaper per turn.

Note: ``api_request_log`` carries the same token columns but is NOT on the
/debug/query table allowlist, so this script reads from ``foreman_turns``
instead, which is allow-listed and holds a JSON copy of the same call-level
usage via the api_calls_json column set by backend/foreman/runner.py.

Usage:
    export DEBUG_TOKEN=...                          # required
    export PIONEER_BACKEND_URL=http://localhost:8000  # optional, this is the default
    python scripts/evaluate_child_context_tokens.py [--since-days N] [--guild-id ID]

Exit codes:
    0  report generated
    1  DEBUG_TOKEN missing, request failed, or no data in range
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta

PAGE_SIZE = 500


def _backend_url() -> str:
    return os.environ.get("PIONEER_BACKEND_URL", "http://localhost:8000").rstrip("/")


def _debug_query(sql: str, *, token: str) -> list[dict]:
    req = urllib.request.Request(
        f"{_backend_url()}/debug/query",
        data=json.dumps({"sql": sql}).encode(),
        headers={"X-Debug-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from /debug/query: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {_backend_url()}: {exc.reason}") from exc


def fetch_assistant_turns(token: str, since_iso: str, guild_id: int | None) -> list[dict]:
    """Page through foreman_turns assistant rows with usage data, oldest first."""
    rows: list[dict] = []
    offset = 0
    guild_clause = f" AND guild_id = {int(guild_id)}" if guild_id is not None else ""
    while True:
        sql = (
            "SELECT task_id, api_calls_json, created_at FROM foreman_turns "
            "WHERE role = 'assistant' AND api_calls_json IS NOT NULL "
            f"AND created_at >= '{since_iso}'{guild_clause} "
            f"ORDER BY created_at LIMIT {PAGE_SIZE} OFFSET {offset}"
        )
        page = _debug_query(sql, token=token)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def bucket_key(task_id: str | None) -> str:
    return "child" if task_id else "parent"


def summarize(rows: list[dict]) -> dict:
    """Return per-bucket (child/parent) turn counts, call counts, and token sums."""
    stats = {
        "child": defaultdict(int, {"turns": 0, "calls": 0}),
        "parent": defaultdict(int, {"turns": 0, "calls": 0}),
    }
    token_fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

    for row in rows:
        bucket = bucket_key(row.get("task_id"))
        stats[bucket]["turns"] += 1

        raw = row.get("api_calls_json")
        if not raw:
            continue
        try:
            calls = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(calls, list):
            continue

        for call in calls:
            if not isinstance(call, dict):
                continue
            stats[bucket]["calls"] += 1
            for field in token_fields:
                stats[bucket][field] += call.get(field) or 0

    return stats


def print_report(stats: dict, since_iso: str, guild_id: int | None) -> None:
    child = stats["child"]
    parent = stats["parent"]

    print("=" * 70)
    print("  FOREMAN_CHILD_CONTEXTS — Token Savings Report")
    print("=" * 70)
    print(f"\nWindow:   since {since_iso}")
    print(f"Guild:    {guild_id if guild_id is not None else 'all guilds'}")
    print("Source:   foreman_turns.api_calls_json (per-call provider-reported usage)")

    total_turns = child["turns"] + parent["turns"]
    if total_turns == 0:
        print("\nNo assistant turns with usage data found in this window.")
        return

    print(f"\n{'Metric':<28} {'Child (task_id set)':>20} {'Parent (guild-wide)':>20}")
    print("-" * 70)
    print(f"{'Turns':<28} {child['turns']:>20} {parent['turns']:>20}")
    print(f"{'API calls':<28} {child['calls']:>20} {parent['calls']:>20}")

    fields = [
        ("Input tokens", "input_tokens"),
        ("Output tokens", "output_tokens"),
        ("Cache read tokens", "cache_read_tokens"),
        ("Cache write tokens", "cache_write_tokens"),
    ]
    for label, field in fields:
        print(f"{label:<28} {child[field]:>20} {parent[field]:>20}")

    child_total = sum(child[f] for _, f in fields)
    parent_total = sum(parent[f] for _, f in fields)
    print(f"{'Total tokens':<28} {child_total:>20} {parent_total:>20}")

    print("\n--- Per-turn averages ---")
    print(f"{'Metric':<28} {'Child':>20} {'Parent':>20}")
    print("-" * 70)
    for label, field in fields:
        c_avg = child[field] / child["turns"] if child["turns"] else 0
        p_avg = parent[field] / parent["turns"] if parent["turns"] else 0
        print(f"{label:<28} {c_avg:>20,.1f} {p_avg:>20,.1f}")
    c_total_avg = child_total / child["turns"] if child["turns"] else 0
    p_total_avg = parent_total / parent["turns"] if parent["turns"] else 0
    print(f"{'Total tokens/turn':<28} {c_total_avg:>20,.1f} {p_total_avg:>20,.1f}")

    print("\n--- Verdict ---")
    if child["turns"] == 0:
        print("No child-context turns in this window — FOREMAN_CHILD_CONTEXTS may be")
        print("disabled, or no task-complete/followup-done/needs-input/task-error events")
        print("have fired yet. Nothing to compare.")
    elif parent["turns"] == 0:
        print("No parent-context turns in this window to compare against.")
    else:
        savings_pct = (1 - (c_total_avg / p_total_avg)) * 100 if p_total_avg else 0
        direction = "less" if savings_pct > 0 else "more"
        print(
            f"Child-context turns average {abs(savings_pct):.1f}% {direction} total tokens "
            "per turn than parent-context turns."
        )
        if savings_pct > 0:
            print("=> FOREMAN_CHILD_CONTEXTS is reducing per-turn token usage in this window.")
        else:
            print("=> FOREMAN_CHILD_CONTEXTS is NOT reducing per-turn token usage in this window.")

    print("\n" + "=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--since-days", type=int, default=14, help="how many days back to analyze (default: 14)"
    )
    parser.add_argument(
        "--guild-id", type=int, default=None, help="restrict to a single guild's integer id"
    )
    args = parser.parse_args()

    token = os.environ.get("DEBUG_TOKEN", "")
    if not token:
        print("ERROR: set DEBUG_TOKEN to the backend's debug token", file=sys.stderr)
        return 1

    since_iso = (datetime.now(UTC) - timedelta(days=args.since_days)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )

    try:
        rows = fetch_assistant_turns(token, since_iso, args.guild_id)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    stats = summarize(rows)
    print_report(stats, since_iso, args.guild_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
