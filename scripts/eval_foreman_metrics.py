#!/usr/bin/env python3
"""General child-foreman effectiveness metrics via the /debug/query endpoint.

Reports, over a configurable time window:

  - Worker spawn success rate      (workers.spawned_version IS NOT NULL vs NULL)
  - Worker lifecycle               (workers.state distribution, online-now count,
                                     avg time from started_at to last_seen)
  - Task assignment/completion     (tasks.state distribution, worker_id assignment rate)
  - Task queue depth               (tasks currently in an active/pending-like state,
                                     plus tasks-created-per-day as a creation trend)
  - Worker utilization             (agents.state distribution, idle vs busy split,
                                     share of agents currently pinned to a task)

Data-shape caveat: ``tasks``/``workers``/``agents`` are point-in-time state
tables — they don't record state-transition history (no started_at/completed_at
on tasks, no per-transition log row). So "queue depth over time" and
"utilization" below are reconstructed from creation/last-seen timestamps and
the *current* state column, not a true time series. Where a metric is a
snapshot rather than a trend, the report says so explicitly.

Usage:
    export DEBUG_TOKEN=...                            # required
    export PIONEER_BACKEND_URL=http://localhost:8000  # optional, this is the default
    python scripts/eval_foreman_metrics.py [--since-days N] [--guild-id ID]

Exit codes:
    0  report generated
    1  DEBUG_TOKEN missing or a request failed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta

PAGE_SIZE = 500
ONLINE_THRESHOLD_MINUTES = 10

_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$")


def _sql_timestamp_literal(iso_str: str) -> str:
    """Validate `iso_str` is a strict UTC ISO-8601 timestamp and quote it as a SQL literal.

    /debug/query takes one raw `sql` string with no bind-parameter support, so this
    is the sanitization boundary: reject anything that isn't exactly this shape
    before it gets interpolated into a query.
    """
    if not _ISO_TIMESTAMP_RE.match(iso_str):
        raise ValueError(f"refusing to interpolate non-ISO-8601 timestamp: {iso_str!r}")
    return f"'{iso_str}'"


def _sql_int_literal(value: int) -> str:
    """Validate `value` is a real int (not just int-like) and render it as a SQL literal."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"expected int, got {value!r}")
    return str(value)


# Mirrors backend/worker_lifecycle.py's own definition of "actively occupying
# a worker slot" — used only to label the queue-depth snapshot, not to filter
# the raw data pulled from the DB.
_ACTIVE_TASK_STATES = ("pending", "working")


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


def _fetch_all(select_sql: str, *, token: str) -> list[dict]:
    """Page through a query (no LIMIT/OFFSET in select_sql) via the 500-row cap."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = _debug_query(f"{select_sql} LIMIT {PAGE_SIZE} OFFSET {offset}", token=token)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def _parse_dt(val) -> datetime | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


def fetch_workers(token: str, since_iso: str, guild_id: int | None) -> list[dict]:
    since_literal = _sql_timestamp_literal(since_iso)
    guild_clause = f" AND guild_id = {_sql_int_literal(guild_id)}" if guild_id is not None else ""
    sql = (
        "SELECT id, spawned_version, state, disabled, created_at, started_at, last_seen "
        f"FROM workers WHERE created_at >= {since_literal}{guild_clause} ORDER BY created_at"
    )
    return _fetch_all(sql, token=token)


def fetch_tasks(token: str, since_iso: str, guild_id: int | None) -> list[dict]:
    since_literal = _sql_timestamp_literal(since_iso)
    guild_clause = f" AND guild_id = {_sql_int_literal(guild_id)}" if guild_id is not None else ""
    sql = (
        "SELECT id, worker_id, state, phase, created_at, deleted_at "
        f"FROM tasks WHERE created_at >= {since_literal}{guild_clause} ORDER BY created_at"
    )
    return _fetch_all(sql, token=token)


def fetch_agents(token: str, since_iso: str, guild_id: int | None) -> list[dict]:
    since_literal = _sql_timestamp_literal(since_iso)
    guild_clause = f" AND guild_id = {_sql_int_literal(guild_id)}" if guild_id is not None else ""
    sql = (
        "SELECT id, worker_id, state, joined_at, last_seen, current_task_id "
        f"FROM agents WHERE joined_at >= {since_literal}{guild_clause} ORDER BY joined_at"
    )
    return _fetch_all(sql, token=token)


# ---------------------------------------------------------------------------
# Report sections
# ---------------------------------------------------------------------------


def report_worker_spawns(workers: list[dict]) -> None:
    print("\n--- Worker Spawn Success Rate ---")
    total = len(workers)
    if total == 0:
        print("No workers created in this window.")
        return

    spawned = sum(1 for w in workers if w.get("spawned_version"))
    failed = total - spawned
    rate = spawned / total * 100

    print(f"Workers created:            {total}")
    print(f"Successfully spawned:       {spawned}  (spawned_version set)")
    print(f"Never came up:              {failed}  (spawned_version NULL)")
    print(f"Spawn success rate:         {rate:.1f}%")

    disabled = sum(1 for w in workers if w.get("disabled"))
    print(f"Disabled workers:           {disabled}")


def report_worker_lifecycle(workers: list[dict]) -> None:
    print("\n--- Worker Lifecycle (state snapshot + online duration) ---")
    total = len(workers)
    if total == 0:
        print("No workers created in this window.")
        return

    state_counts = Counter(w.get("state") or "unknown" for w in workers)
    print(f"{'State':<15} {'Count':>7} {'Share':>8}")
    print("-" * 32)
    for state, count in state_counts.most_common():
        print(f"{state:<15} {count:>7} {count / total * 100:>7.1f}%")

    now = datetime.now(UTC)
    online_now = 0
    durations_hours = []
    for w in workers:
        last_seen = _parse_dt(w.get("last_seen"))
        started = _parse_dt(w.get("started_at"))
        if last_seen and (now - last_seen) <= timedelta(minutes=ONLINE_THRESHOLD_MINUTES):
            online_now += 1
        if started and last_seen and last_seen >= started:
            durations_hours.append((last_seen - started).total_seconds() / 3600)

    print(f"\nOnline now (last_seen <= {ONLINE_THRESHOLD_MINUTES}min ago): {online_now}/{total}")
    if durations_hours:
        avg_hours = sum(durations_hours) / len(durations_hours)
        print(f"Avg started_at -> last_seen span: {avg_hours:.2f}h (n={len(durations_hours)})")
    else:
        print("Avg started_at -> last_seen span: n/a (no workers with both timestamps)")


def report_tasks(tasks: list[dict]) -> None:
    print("\n--- Task Assignment / Completion (current-state snapshot) ---")
    active = [t for t in tasks if not t.get("deleted_at")]
    total = len(active)
    if total == 0:
        print("No tasks created in this window.")
        return

    assigned = sum(1 for t in active if t.get("worker_id"))
    print(f"Tasks created:               {total}")
    print(f"Assigned to a worker:        {assigned}  ({assigned / total * 100:.1f}%)")
    print(f"Soft-deleted in window:      {len(tasks) - total}")

    state_counts = Counter(t.get("state") or "unknown" for t in active)
    print(f"\n{'State':<20} {'Count':>7} {'Share':>8}")
    print("-" * 37)
    for state, count in state_counts.most_common():
        print(f"{state:<20} {count:>7} {count / total * 100:>7.1f}%")

    phase_counts = Counter(t.get("phase") or "unknown" for t in active)
    print(f"\n{'Phase':<20} {'Count':>7} {'Share':>8}")
    print("-" * 37)
    for phase, count in phase_counts.most_common():
        print(f"{phase:<20} {count:>7} {count / total * 100:>7.1f}%")


def report_queue_depth(tasks: list[dict]) -> None:
    print("\n--- Task Queue Depth ---")
    active = [t for t in tasks if not t.get("deleted_at")]
    if not active:
        print("No tasks created in this window.")
        return

    pending_like = [t for t in active if (t.get("state") or "") in _ACTIVE_TASK_STATES]
    print(
        f"Currently pending/working ({'/'.join(_ACTIVE_TASK_STATES)}): "
        f"{len(pending_like)}/{len(active)} (snapshot, not a time series)"
    )

    per_day = Counter()
    for t in active:
        created = _parse_dt(t.get("created_at"))
        if created:
            per_day[created.date().isoformat()] += 1

    print("\nTasks created per day (creation-time trend, not queue length):")
    for day, count in sorted(per_day.items()):
        bar = "#" * min(count, 50)
        print(f"  {day}  {count:>4}  {bar}")


def report_worker_utilization(agents: list[dict]) -> None:
    print("\n--- Worker Utilization (agents.state snapshot) ---")
    total = len(agents)
    if total == 0:
        print("No agents joined in this window.")
        return

    state_counts = Counter(a.get("state") or "unknown" for a in agents)
    print(f"{'State':<15} {'Count':>7} {'Share':>8}")
    print("-" * 32)
    for state, count in state_counts.most_common():
        print(f"{state:<15} {count:>7} {count / total * 100:>7.1f}%")

    pinned = sum(1 for a in agents if a.get("current_task_id"))
    print(f"\nAgents currently pinned to a task: {pinned}/{total} ({pinned / total * 100:.1f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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
        workers = fetch_workers(token, since_iso, args.guild_id)
        tasks = fetch_tasks(token, since_iso, args.guild_id)
        agents = fetch_agents(token, since_iso, args.guild_id)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("=" * 60)
    print("  Child Foreman Effectiveness Report")
    print("=" * 60)
    print(f"\nWindow:   since {since_iso}")
    print(f"Guild:    {args.guild_id if args.guild_id is not None else 'all guilds'}")

    report_worker_spawns(workers)
    report_worker_lifecycle(workers)
    report_tasks(tasks)
    report_queue_depth(tasks)
    report_worker_utilization(agents)

    print("\n" + "=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
