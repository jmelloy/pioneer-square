"""Tests for Phase 2 GitHub webhook plumbing.

Covers:
- Foreman dispatch + filtering rules (``_should_dispatch_to_foreman``,
  ``_build_foreman_summary``)
- The end-to-end receiver-to-foreman path (verifying ``run_foreman_ai``
  is invoked when a webhook arrives that matches a known task)
- The new ``get_pr_status`` foreman tool
- The system prompt section that teaches the foreman how to react
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
from foreman.tools import exec_tools  # noqa: E402
from helpers import create_db, insert_guild, make_auth_token, raw_conn  # noqa: E402
from routes.webhooks import (  # noqa: E402
    _build_foreman_summary,
    _should_dispatch_to_foreman,
)

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pioneer:pioneer_password@localhost/pioneer_test",
)

# ---------------------------------------------------------------------------
# Helpers (mirror test_github_webhooks.py to keep these focused on Phase 2)
# ---------------------------------------------------------------------------


def _set_webhook_secret(db_url: str, guild_id: str, secret: str) -> None:
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "UPDATE guilds SET webhook_secret = %s WHERE guild_id = %s",
            (secret, guild_id),
        )


def _insert_task(
    db_url: str,
    *,
    task_id: str,
    guild_id: str,
    pr_url: str | None = None,
    pr_number: int | None = None,
    pr_repo: str | None = None,
    user_id: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute("SELECT id FROM guilds WHERE guild_id = %s AND deleted_at IS NULL", (guild_id,))
        row = cur.fetchone()
        guild_pk = row["id"]
        cur.execute(
            "INSERT INTO workers (id, guild_pk, repos, state, created_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (f"w-{task_id}", guild_pk, "[]", "online", now),
        )
        cur.execute(
            "INSERT INTO tasks (id, worker_id, guild_pk, description, tool, state, "
            "pr_url, pr_number, pr_repo, user_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                task_id,
                f"w-{task_id}",
                guild_pk,
                "test task",
                "claude",
                "awaiting-review",
                pr_url,
                pr_number,
                pr_repo,
                user_id,
                now,
            ),
        )


def _signed_headers(secret: str, body: bytes, *, event: str, delivery: str) -> dict[str, str]:
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={sig}",
        "Content-Type": "application/json",
    }


def _pr_payload(*, action: str, repo: str, number: int, **extra) -> dict:
    pr = {
        "number": number,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        **extra.pop("pull_request_extra", {}),
    }
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": pr,
        "sender": {"login": extra.pop("sender_login", "octocat")},
        **extra,
    }


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class TestShouldDispatch:
    def test_no_task_skips(self):
        ok, reason = _should_dispatch_to_foreman("pull_request", "opened", {}, "alice", None)
        assert not ok
        assert reason == "no-matching-task"

    def test_bot_sender_on_non_ci_skips(self):
        ok, reason = _should_dispatch_to_foreman(
            "issue_comment", "created", {}, "dependabot[bot]", "t-1"
        )
        assert not ok
        assert reason == "bot-non-ci-sender"

    def test_bot_sender_on_check_run_dispatches(self):
        payload = {"check_run": {"status": "completed", "conclusion": "failure"}}
        ok, _ = _should_dispatch_to_foreman(
            "check_run", "completed", payload, "github-actions[bot]", "t-1"
        )
        assert ok

    def test_pending_check_run_skips(self):
        payload = {"check_run": {"status": "in_progress"}}
        ok, reason = _should_dispatch_to_foreman("check_run", "created", payload, "ci[bot]", "t-1")
        assert not ok
        assert reason == "check-status-in_progress"

    def test_neutral_check_conclusion_skips(self):
        payload = {"check_run": {"status": "completed", "conclusion": "neutral"}}
        ok, reason = _should_dispatch_to_foreman(
            "check_run", "completed", payload, "ci[bot]", "t-1"
        )
        assert not ok
        assert reason == "check-conclusion-neutral"

    def test_skipped_check_conclusion_skips(self):
        payload = {"check_suite": {"status": "completed", "conclusion": "skipped"}}
        ok, _ = _should_dispatch_to_foreman("check_suite", "completed", payload, "ci[bot]", "t-1")
        assert not ok

    def test_human_review_dispatches(self):
        ok, _ = _should_dispatch_to_foreman(
            "pull_request_review", "submitted", {}, "human-reviewer", "t-1"
        )
        assert ok


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_pr_merged(self):
        payload = {"pull_request": {"merged": True}}
        s = _build_foreman_summary(
            "pull_request", "closed", payload, "owner/repo", 42, "t-1", "human"
        )
        assert "[github-event] pull_request/closed on owner/repo#42 (task t-1)" in s
        assert "merged" in s.lower()
        assert "finalize_task" in s

    def test_pr_closed_unmerged(self):
        payload = {"pull_request": {"merged": False}}
        s = _build_foreman_summary(
            "pull_request", "closed", payload, "owner/repo", 42, "t-1", "human"
        )
        assert "without merging" in s.lower()
        assert "send_followup" in s

    def test_review_changes_requested(self):
        payload = {"review": {"state": "changes_requested", "body": "needs work"}}
        s = _build_foreman_summary(
            "pull_request_review", "submitted", payload, "o/r", 1, "t-1", "alice"
        )
        assert "changes_requested" in s
        assert "needs work" in s
        assert "send_followup" in s

    def test_review_approved(self):
        payload = {"review": {"state": "approved", "body": ""}}
        s = _build_foreman_summary(
            "pull_request_review", "submitted", payload, "o/r", 1, "t-1", "alice"
        )
        assert "approved" in s

    def test_check_run_failure(self):
        payload = {
            "check_run": {
                "name": "rspec",
                "conclusion": "failure",
                "output": {"summary": "3 tests failed in spec/auth_spec.rb"},
            }
        }
        s = _build_foreman_summary("check_run", "completed", payload, "o/r", 9, "t-2", "ci[bot]")
        assert "rspec" in s
        assert "failure" in s
        assert "3 tests failed" in s
        assert "send_followup" in s

    def test_check_run_success(self):
        payload = {"check_run": {"name": "rspec", "conclusion": "success"}}
        s = _build_foreman_summary("check_run", "completed", payload, "o/r", 9, "t-2", "ci[bot]")
        assert "success" in s
        assert "finalize_task" in s

    def test_issue_comment(self):
        payload = {"comment": {"body": "please rebase"}}
        s = _build_foreman_summary("issue_comment", "created", payload, "o/r", 9, "t-2", "alice")
        assert "please rebase" in s

    def test_review_comment_includes_path(self):
        payload = {"comment": {"path": "src/app.py", "body": "this is wrong"}}
        s = _build_foreman_summary(
            "pull_request_review_comment", "created", payload, "o/r", 9, "t-2", "alice"
        )
        assert "src/app.py" in s
        assert "this is wrong" in s


# ---------------------------------------------------------------------------
# End-to-end: webhook → foreman dispatch
# ---------------------------------------------------------------------------


def test_webhook_dispatches_to_foreman_when_task_matches(client):
    test_client, db_url = client
    insert_guild(db_url, "gd1")
    _set_webhook_secret(db_url, "gd1", "ssecret")
    _insert_task(
        db_url,
        task_id="t-dispatch-1",
        guild_id="gd1",
        pr_url="https://github.com/o/r/pull/3",
        pr_number=3,
        pr_repo="o/r",
        user_id="user-42",
    )
    payload = _pr_payload(
        action="closed", repo="o/r", number=3, pull_request_extra={"merged": True}
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("ssecret", body, event="pull_request", delivery="d-disp-1")
    with (
        patch("routes.webhooks.spawn") as mock_spawn,
        patch("routes.webhooks.reset_foreman_poll") as mock_reset,
    ):
        resp = test_client.post("/webhooks/github/gd1", content=body, headers=headers)
    assert resp.status_code == 202
    # spawn() called once, with run_foreman_ai coroutine
    assert mock_spawn.call_count == 1
    call_kwargs = mock_spawn.call_args
    coro = call_kwargs.args[0]
    assert hasattr(coro, "send")  # it's a coroutine
    coro.close()  # so asyncio doesn't whine about a never-awaited coroutine
    assert mock_reset.call_count == 1


def test_webhook_skips_foreman_when_no_task_match(client):
    test_client, db_url = client
    insert_guild(db_url, "gd2")
    _set_webhook_secret(db_url, "gd2", "ssecret")
    payload = _pr_payload(action="opened", repo="o/r", number=99)
    body = json.dumps(payload).encode()
    headers = _signed_headers("ssecret", body, event="pull_request", delivery="d-disp-2")
    with patch("routes.webhooks.spawn") as mock_spawn:
        resp = test_client.post("/webhooks/github/gd2", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_spawn.call_count == 0


def test_webhook_skips_foreman_for_bot_on_non_ci(client):
    test_client, db_url = client
    insert_guild(db_url, "gd3")
    _set_webhook_secret(db_url, "gd3", "ssecret")
    _insert_task(
        db_url,
        task_id="t-bot",
        guild_id="gd3",
        pr_url="https://github.com/o/r/pull/5",
        pr_number=5,
        pr_repo="o/r",
    )
    payload = {
        "action": "created",
        "repository": {"full_name": "o/r"},
        "issue": {
            "number": 5,
            "html_url": "https://github.com/o/r/pull/5",
            "pull_request": {},
        },
        "comment": {"body": "Automated comment"},
        "sender": {"login": "dependabot[bot]"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("ssecret", body, event="issue_comment", delivery="d-bot")
    with patch("routes.webhooks.spawn") as mock_spawn:
        resp = test_client.post("/webhooks/github/gd3", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_spawn.call_count == 0


def test_webhook_dispatches_for_ci_bot_check_run(client):
    test_client, db_url = client
    insert_guild(db_url, "gd4")
    _set_webhook_secret(db_url, "gd4", "ssecret")
    _insert_task(
        db_url,
        task_id="t-ci",
        guild_id="gd4",
        pr_url="https://github.com/o/r/pull/7",
        pr_number=7,
        pr_repo="o/r",
    )
    payload = {
        "action": "completed",
        "repository": {"full_name": "o/r"},
        "check_run": {
            "name": "rspec",
            "status": "completed",
            "conclusion": "failure",
            "output": {"summary": "boom"},
            "pull_requests": [{"number": 7, "html_url": "https://github.com/o/r/pull/7"}],
        },
        "sender": {"login": "github-actions[bot]"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("ssecret", body, event="check_run", delivery="d-ci-1")
    with (
        patch("routes.webhooks.spawn") as mock_spawn,
        patch("routes.webhooks.reset_foreman_poll"),
    ):
        resp = test_client.post("/webhooks/github/gd4", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_spawn.call_count == 1
    coro = mock_spawn.call_args.args[0]
    coro.close()


# ---------------------------------------------------------------------------
# get_pr_status tool
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(monkeypatch):
    from helpers import truncate_all

    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    yield db_url


def _fake_tool_use(name: str, inputs: dict, tool_id: str = "tool-1"):
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


@pytest.mark.asyncio
async def test_get_pr_status_returns_reviews_and_checks(db_session):
    insert_guild(db_session, "g-prs")
    fake_pr = {
        "number": 42,
        "state": "open",
        "merged": False,
        "mergeable": True,
        "draft": False,
        "head": {"sha": "deadbeef"},
    }
    fake_reviews = [
        {
            "user": {"login": "alice"},
            "state": "approved",
            "body": "lgtm",
            "submitted_at": "2026-05-06T00:00:00Z",
        }
    ]
    fake_checks = {
        "check_runs": [
            {
                "name": "rspec",
                "status": "completed",
                "conclusion": "success",
                "output": {"summary": "100/100 passed"},
            }
        ]
    }
    responses = iter([fake_pr, fake_reviews, fake_checks])
    with (
        patch("foreman.tools.broadcast", new_callable=AsyncMock),
        patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
        patch("foreman.tools._gh_api", side_effect=lambda *a, **kw: next(responses)),
    ):
        results = await exec_tools(
            "g-prs", [_fake_tool_use("get_pr_status", {"repo": "o/r", "pr_number": 42})]
        )
    parsed = json.loads(results[0]["content"])
    assert parsed["number"] == 42
    assert parsed["merged"] is False
    assert parsed["head_sha"] == "deadbeef"
    assert parsed["reviews"][0]["user"] == "alice"
    assert parsed["reviews"][0]["state"] == "approved"
    assert parsed["checks"][0]["name"] == "rspec"
    assert parsed["checks"][0]["conclusion"] == "success"


@pytest.mark.asyncio
async def test_get_pr_status_skips_check_runs_when_no_head_sha(db_session):
    """If the PR has no head.sha (rare, but possible for ghost branches) the
    check-runs API call must be skipped, not retried with a None SHA."""
    insert_guild(db_session, "g-prs2")
    fake_pr = {"number": 1, "state": "closed", "merged": False, "head": {}}
    fake_reviews: list = []
    responses = iter([fake_pr, fake_reviews])
    with (
        patch("foreman.tools.broadcast", new_callable=AsyncMock),
        patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
        patch("foreman.tools._gh_api", side_effect=lambda *a, **kw: next(responses)),
    ):
        results = await exec_tools(
            "g-prs2", [_fake_tool_use("get_pr_status", {"repo": "o/r", "pr_number": 1})]
        )
    parsed = json.loads(results[0]["content"])
    assert parsed["checks"] == []


def test_get_pr_status_in_tool_definitions():
    """Sanity check: tool is registered so the foreman can call it."""
    from foreman.tools import FOREMAN_TOOLS

    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "get_pr_status" in names


def test_prompt_describes_github_events():
    """The system prompt should teach the foreman about [github-event] triggers."""
    from foreman.prompt import FOREMAN_SYSTEM, build_system_prompt

    assert "[github-event]" in FOREMAN_SYSTEM
    assert "get_pr_status" in FOREMAN_SYSTEM
    rendered = build_system_prompt("[]", "[]")
    assert "github-event" in rendered
