#!/usr/bin/env python3
"""Live integration test for A2A agent calls and the dnsid-py library.

Tests:
  - code-review-agent at https://agent.meyers.life (always attempted)
  - dnsid-py library (resolve, sign, verify operations)

Exit codes:
  0  All reachable agents/tools passed
  1  At least one reachable check failed

Usage:
    python scripts/test_a2a_live.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

REVIEW_AGENT_URL = "https://agent.meyers.life"
TIMEOUT = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch(url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


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

    return results


def test_dnsid_py() -> list[dict]:
    results: list[dict] = []
    print("\n=== dnsid-py library ===")

    # 1. resolve — look up a well-known domain
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
        from foreman.tools import _dnsid_resolve

        out = _dnsid_resolve("dnsid.pioneer-square.melloy.life")
        ok = out.get("ok") is True and "fqdn" in out
        results.append(_result("resolve dnsid.pioneer-square.melloy.life", ok, str(out)[:120]))
    except Exception as exc:
        results.append(_result("resolve dnsid.pioneer-square.melloy.life", False, str(exc)))

    # 2. sign — requires DNSID_PRIVATE_KEY_PEM env var; skip if not set
    pem = os.environ.get("DNSID_PRIVATE_KEY_PEM", "")
    if not pem:
        results.append(
            _skip("sign (no DNSID_PRIVATE_KEY_PEM)", "set DNSID_PRIVATE_KEY_PEM to test")
        )
    else:
        try:
            import time

            from foreman.auth import _dnsid_sign_sync

            claims = {
                "iss": "dnsid.pioneer-square.melloy.life",
                "sub": "dnsid.pioneer-square.melloy.life",
                "aud": "test-aud",
                "exp": int(time.time()) + 300,
            }
            jwt_token = _dnsid_sign_sync(claims, pem)
            ok = isinstance(jwt_token, str) and jwt_token.count(".") == 2
            results.append(_result("sign with PEM key", ok, jwt_token[:80]))
        except Exception as exc:
            results.append(_result("sign with PEM key", False, str(exc)))

    # 3. verify — skip if no PEM key (nothing to sign with)
    if not pem:
        results.append(
            _skip("verify (no DNSID_PRIVATE_KEY_PEM)", "set DNSID_PRIVATE_KEY_PEM to test")
        )
    else:
        try:
            import time

            from foreman.auth import _dnsid_sign_sync
            from foreman.tools import _dnsid_verify

            claims = {
                "iss": "dnsid.pioneer-square.melloy.life",
                "sub": "dnsid.pioneer-square.melloy.life",
                "aud": "test-aud",
                "exp": int(time.time()) + 300,
            }
            jwt_token = _dnsid_sign_sync(claims, pem)
            out = _dnsid_verify(jwt_token, "test-aud")
            ok = out.get("ok") is True and out.get("iss") == "dnsid.pioneer-square.melloy.life"
            results.append(_result("verify signed token", ok, str(out)[:120]))
        except Exception as exc:
            results.append(_result("verify signed token", False, str(exc)))

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    all_results: list[dict] = []

    all_results.extend(test_review_agent(REVIEW_AGENT_URL))
    all_results.extend(test_dnsid_py())

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
