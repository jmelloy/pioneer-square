#!/usr/bin/env python3
"""Live integration test for A2A agent calls and the dnsid-sdk CLI.

Tests:
  - code-review-agent at https://agent.meyers.life (always attempted)
  - dnsid-sdk CLI at DNSID_SDK_BIN (default ~/dnsid-go/bin/dnsid-sdk, skipped if not found)

Exit codes:
  0  All reachable agents/tools passed
  1  At least one reachable check failed

Usage:
    python scripts/test_a2a_live.py
    DNSID_SDK_BIN=/custom/path/dnsid-sdk python scripts/test_a2a_live.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REVIEW_AGENT_URL = "https://agent.meyers.life"
DNSID_BIN = os.path.expanduser(os.environ.get("DNSID_SDK_BIN", "~/dnsid-go/bin/dnsid-sdk"))
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


def _run_dnsid(*args, stdin_data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DNSID_BIN, *args],
        input=stdin_data,
        capture_output=True,
        timeout=10,
    )


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


def test_dnsid_cli() -> list[dict]:
    results: list[dict] = []
    print(f"\n=== dnsid-sdk CLI @ {DNSID_BIN} ===")

    # 0. Check binary exists
    if not os.path.isfile(DNSID_BIN):
        results.append(_skip("dnsid-sdk suite", f"binary not found at {DNSID_BIN}"))
        return results

    # 1. resolve — look up a well-known domain
    try:
        proc = _run_dnsid("resolve", "nyoyny.pioneer-square.melloy.life")
        out = json.loads(proc.stdout)
        ok = out.get("ok") is True and "fqdn" in out
        results.append(_result("resolve nyoyny.pioneer-square.melloy.life", ok, str(out)[:120]))
    except subprocess.TimeoutExpired:
        results.append(_result("resolve nyoyny.pioneer-square.melloy.life", False, "timed out"))
    except Exception as exc:
        results.append(_result("resolve nyoyny.pioneer-square.melloy.life", False, str(exc)))

    # 2. sign — requires DNSID_AGENT_CONFIG; skip if not set
    config_path = os.environ.get("DNSID_AGENT_CONFIG", "")
    if not config_path:
        results.append(_skip("sign (no DNSID_AGENT_CONFIG)", "set DNSID_AGENT_CONFIG to test"))
    else:
        try:
            import time

            claims = {
                "iss": "nyoyny.pioneer-square.melloy.life",
                "sub": "nyoyny.pioneer-square.melloy.life",
                "aud": "test-aud",
                "exp": int(time.time()) + 300,
            }
            proc = _run_dnsid(
                "sign", "--config", config_path, stdin_data=json.dumps(claims).encode()
            )
            out = json.loads(proc.stdout)
            ok = out.get("ok") is True and isinstance(out.get("jwt"), str)
            results.append(_result("sign with config", ok, str(out)[:120]))
        except Exception as exc:
            results.append(_result("sign with config", False, str(exc)))

    # 3. verify — skip if no config (nothing to sign with)
    if not config_path:
        results.append(_skip("verify (no DNSID_AGENT_CONFIG)", "set DNSID_AGENT_CONFIG to test"))
    else:
        try:
            import time

            claims = {
                "iss": "nyoyny.pioneer-square.melloy.life",
                "sub": "nyoyny.pioneer-square.melloy.life",
                "aud": "test-aud",
                "exp": int(time.time()) + 300,
            }
            sign_proc = _run_dnsid(
                "sign", "--config", config_path, stdin_data=json.dumps(claims).encode()
            )
            sign_out = json.loads(sign_proc.stdout)
            if not sign_out.get("ok"):
                results.append(_result("verify (sign step)", False, str(sign_out)[:120]))
            else:
                jwt_token = sign_out["jwt"]
                verify_proc = _run_dnsid(
                    "verify", "--jwt", jwt_token, "--expected-aud", "test-aud"
                )
                out = json.loads(verify_proc.stdout)
                ok = out.get("ok") is True and out.get("iss") == "nyoyny.pioneer-square.melloy.life"
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
    all_results.extend(test_dnsid_cli())

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
