#!/usr/bin/env python3
"""Live integration test for A2A agent calls.

Tests that the Foreman's call_agent tool can reach real A2A agents:
  - code-review-agent at https://agent.meyers.life (always attempted)
  - dnsid-go agent at DNSID_AGENT_URL (default http://localhost:8080, skipped if unreachable)

Exit codes:
  0  All reachable agents passed
  1  At least one reachable agent failed

Usage:
    python scripts/test_a2a_live.py
    DNSID_AGENT_URL=http://myhost:9090 python scripts/test_a2a_live.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

REVIEW_AGENT_URL = "https://agent.meyers.life"
DNSID_AGENT_URL = os.environ.get("DNSID_AGENT_URL", "http://localhost:8080")
TIMEOUT = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch(url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def _post_task(agent_url: str, skill_id: str, params: dict) -> dict:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tasks/send",
            "params": {
                "skill_id": skill_id,
                "message": {"parts": [{"type": "text", "text": json.dumps(params)}]},
            },
            "id": 1,
        }
    ).encode()
    return _fetch(
        f"{agent_url.rstrip('/')}/a2a",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )


def _result(label: str, passed: bool, detail: str = "") -> dict:
    status = "PASS" if passed else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return {"label": label, "passed": passed, "detail": detail}


def _skip(label: str, reason: str) -> dict:
    print(f"[SKIP] {label} — {reason}")
    return {"label": label, "passed": True, "skipped": True, "detail": reason}


# ---------------------------------------------------------------------------
# Test suites
# ---------------------------------------------------------------------------


def test_review_agent(agent_url: str) -> list[dict]:
    results: list[dict] = []
    print(f"\n=== code-review-agent @ {agent_url} ===")

    # 1. Fetch agent card
    try:
        card = _fetch(f"{agent_url}/.well-known/agent.json")
        results.append(_result("agent card fetched", True, f"name={card.get('name', '?')!r}"))
    except urllib.error.HTTPError as exc:
        results.append(_result("agent card fetched", False, f"HTTP {exc.code}"))
        return results
    except OSError as exc:
        results.append(_result("agent card fetched", False, str(exc)))
        return results

    # 2. Verify card structure
    has_skills = bool(card.get("skills"))
    results.append(
        _result(
            "agent card has skills", has_skills, str([s.get("id") for s in card.get("skills", [])])
        )
    )

    # 3. Check for review_pr / pr-review skill
    skill_ids = [s.get("id", "") for s in card.get("skills", [])]
    has_review = any("review" in sid for sid in skill_ids)
    results.append(_result("review skill present", has_review, f"skills={skill_ids}"))

    # 4. Call the MCP tools/list endpoint to verify it responds to JSON-RPC
    try:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": "ping", "method": "tools/list", "params": {}}
        ).encode()
        resp = _fetch(
            agent_url,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        has_tools = "result" in resp or "error" in resp
        results.append(_result("MCP tools/list responds", has_tools))
    except Exception as exc:
        results.append(_result("MCP tools/list responds", False, str(exc)))

    return results


def test_dnsid_agent(agent_url: str) -> list[dict]:
    results: list[dict] = []
    print(f"\n=== dnsid-go agent @ {agent_url} ===")

    # 1. Fetch agent card (skip entire suite if unreachable)
    try:
        card = _fetch(f"{agent_url}/.well-known/agent.json", headers={"Accept": "application/json"})
        results.append(_result("agent card fetched", True, f"name={card.get('name', '?')!r}"))
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", str(exc))
        if "refused" in str(reason).lower() or "timed out" in str(reason).lower():
            results.append(_skip("dnsid-go suite", f"agent not reachable at {agent_url}: {reason}"))
            return results
        results.append(_result("agent card fetched", False, str(exc)))
        return results
    except urllib.error.HTTPError as exc:
        results.append(_result("agent card fetched", False, f"HTTP {exc.code}"))
        return results

    # 2. Card has required skills
    skill_ids = [s.get("id", "") for s in card.get("skills", [])]
    has_verify = "verify_identity_record" in skill_ids
    has_lookup = "lookup_dnsid" in skill_ids
    results.append(
        _result("verify_identity_record skill present", has_verify, f"skills={skill_ids}")
    )
    results.append(_result("lookup_dnsid skill present", has_lookup, f"skills={skill_ids}"))

    # 3. Call lookup_dnsid with a known safe domain
    if has_lookup:
        try:
            resp = _post_task(agent_url, "lookup_dnsid", {"domain": "example.com"})
            ok = "result" in resp or "error" not in resp
            results.append(_result("lookup_dnsid call returns response", ok, str(resp)[:120]))
        except urllib.error.HTTPError as exc:
            results.append(_result("lookup_dnsid call returns response", False, f"HTTP {exc.code}"))
        except Exception as exc:
            results.append(_result("lookup_dnsid call returns response", False, str(exc)))
    else:
        results.append(_skip("lookup_dnsid call", "skill not advertised"))

    # 4. Call verify_identity_record
    if has_verify:
        try:
            resp = _post_task(agent_url, "verify_identity_record", {"domain": "example.com"})
            ok = "result" in resp or "error" not in resp
            results.append(
                _result("verify_identity_record call returns response", ok, str(resp)[:120])
            )
        except urllib.error.HTTPError as exc:
            results.append(
                _result("verify_identity_record call returns response", False, f"HTTP {exc.code}")
            )
        except Exception as exc:
            results.append(_result("verify_identity_record call returns response", False, str(exc)))
    else:
        results.append(_skip("verify_identity_record call", "skill not advertised"))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    all_results: list[dict] = []

    all_results.extend(test_review_agent(REVIEW_AGENT_URL))
    all_results.extend(test_dnsid_agent(DNSID_AGENT_URL))

    non_skip = [r for r in all_results if not r.get("skipped")]
    passed = sum(1 for r in non_skip if r["passed"])
    failed = sum(1 for r in non_skip if not r["passed"])
    skipped = sum(1 for r in all_results if r.get("skipped"))

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

    if failed:
        print("\nFailed checks:")
        for r in non_skip:
            if not r["passed"]:
                print(f"  - {r['label']}: {r.get('detail', '')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
