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

import asyncio
import hashlib
import hmac
import json
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
from _test_config import TEST_DATABASE_URL  # noqa: E402
from discord_notifier import (  # noqa: E402
    linked_issue_number_from_body as _linked_issue_number_from_body,
)
from discord_notifier import (  # noqa: E402
    linked_issue_number_from_branch as _linked_issue_number_from_branch,
)
from foreman.tools import exec_tools  # noqa: E402
from helpers import (  # noqa: E402
    _sync_session,
    create_db,
    insert_guild,
    insert_task,
    insert_worker,
    make_auth_token,
)
from models import (
    Guild,  # noqa: E402
    Task,  # noqa: E402
)
from routes.webhooks import (  # noqa: E402
    _build_foreman_summary,
    _check_run_head_branch,
    _devready_issue_trigger,
    _get_guild_owner_github_login,
    _should_dispatch_to_foreman,
    _should_notify_ci_result,
)
from sqlalchemy import update  # noqa: E402
from sqlmodel import col, select  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers (mirror test_github_webhooks.py to keep these focused on Phase 2)
# ---------------------------------------------------------------------------


def _set_webhook_secret(db_url: str, guild_id: str, secret: str) -> None:

    with _sync_session(db_url) as session:
        session.execute(
            update(Guild).where(col(Guild.slug) == guild_id).values(webhook_secret=secret)
        )
        session.commit()


def _insert_task_with_worker(
    db_url: str,
    *,
    task_id: str,
    guild_id: str,
    pr_url: str | None = None,
    pr_number: int | None = None,
    pr_repo: str | None = None,
    user_id: str | None = None,
) -> None:
    worker_id = f"w-{task_id}"
    insert_worker(db_url, guild_id, worker_id, state="online")
    insert_task(
        db_url,
        guild_id,
        task_id,
        worker_id=worker_id,
        state="awaiting-review",
        pr_url=pr_url,
        pr_number=pr_number,
        pr_repo=pr_repo,
        user_id=user_id,
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


class TestCheckRunHeadBranch:
    """``_check_run_head_branch`` pulls the head branch out of either event shape."""

    def test_check_suite_carries_head_branch_directly(self):
        node = {"head_branch": "main"}
        assert _check_run_head_branch("check_suite", node) == "main"

    def test_check_run_nests_head_branch_under_check_suite(self):
        node = {"check_suite": {"head_branch": "feature/x"}}
        assert _check_run_head_branch("check_run", node) == "feature/x"

    def test_check_run_missing_check_suite_returns_none(self):
        assert _check_run_head_branch("check_run", {}) is None


class TestShouldNotifyCiResult:
    """Main only notifies on failure; PR branches notify on every conclusion."""

    def test_main_failure_notifies(self):
        assert _should_notify_ci_result("main", "failure") is True

    def test_main_success_suppressed(self):
        assert _should_notify_ci_result("main", "success") is False

    def test_main_cancelled_suppressed(self):
        assert _should_notify_ci_result("main", "cancelled") is False

    def test_pr_branch_success_notifies(self):
        assert _should_notify_ci_result("feature/x", "success") is True

    def test_pr_branch_failure_notifies(self):
        assert _should_notify_ci_result("feature/x", "failure") is True

    def test_unknown_branch_defaults_to_notifying(self):
        assert _should_notify_ci_result(None, "success") is True

    def test_irrelevant_conclusion_is_never_notified(self):
        assert _should_notify_ci_result("feature/x", "neutral") is False
        assert _should_notify_ci_result("main", "neutral") is False


# ---------------------------------------------------------------------------
# Linked-issue extraction (branch name / PR body)
# ---------------------------------------------------------------------------


class TestLinkedIssueFromBranch:
    def test_extracts_issue_number_before_task_suffix(self):
        assert (
            _linked_issue_number_from_branch(
                "claude/discord-associate-pr-threads-with-issue-thread-773-t-0jlq"
            )
            == 773
        )

    def test_extracts_from_short_branch(self):
        assert _linked_issue_number_from_branch("claude/fix-thing-123-t-abc123") == 123

    def test_no_task_suffix_returns_none(self):
        assert _linked_issue_number_from_branch("claude/fix-thing-123") is None

    def test_none_branch_returns_none(self):
        assert _linked_issue_number_from_branch(None) is None

    def test_non_matching_branch_returns_none(self):
        assert _linked_issue_number_from_branch("main") is None


class TestLinkedIssueFromBody:
    def test_extracts_closes_reference(self):
        assert _linked_issue_number_from_body("Some PR body.\n\nCloses #42") == 42

    def test_case_insensitive_and_variants(self):
        assert _linked_issue_number_from_body("this fixed #7") == 7
        assert _linked_issue_number_from_body("RESOLVES #99") == 99

    def test_no_reference_returns_none(self):
        assert _linked_issue_number_from_body("Just a description, no linkage.") is None

    def test_none_body_returns_none(self):
        assert _linked_issue_number_from_body(None) is None


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
        assert "automatically finalized" in s

    def test_pr_closed_unmerged(self):
        payload = {"pull_request": {"merged": False}}
        s = _build_foreman_summary(
            "pull_request", "closed", payload, "owner/repo", 42, "t-1", "human"
        )
        assert "without merging" in s.lower()
        assert "automatically marked as failed" in s

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
    _insert_task_with_worker(
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
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd1", content=body, headers=headers)
    assert resp.status_code == 202
    # _debounce_queue.schedule called once with the right guild + user
    assert mock_q.schedule.call_count == 1
    key_arg, guild_arg, summary_arg, user_arg = mock_q.schedule.call_args.args
    assert "gd1" in key_arg
    assert guild_arg == "gd1"
    assert "pull_request" in summary_arg
    assert user_arg == "user-42"


def _get_task_state(db_url: str, task_id: str) -> str | None:
    """Read a task's current state directly from the DB."""
    with _sync_session(db_url) as session:
        return session.scalar(select(col(Task.state)).where(col(Task.id) == task_id))


def test_pr_merge_webhook_auto_finalizes_task(client):
    """PR merge webhook must directly set task state to 'done' without AI involvement."""
    test_client, db_url = client
    insert_guild(db_url, "gd-merge-1")
    _set_webhook_secret(db_url, "gd-merge-1", "secret-m1")
    _insert_task_with_worker(
        db_url,
        task_id="t-merge-1",
        guild_id="gd-merge-1",
        pr_url="https://github.com/o/r/pull/10",
        pr_number=10,
        pr_repo="o/r",
    )
    payload = _pr_payload(
        action="closed", repo="o/r", number=10, pull_request_extra={"merged": True}
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-m1", body, event="pull_request", delivery="d-merge-1")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-merge-1", content=body, headers=headers)
    assert resp.status_code == 202
    assert _get_task_state(db_url, "t-merge-1") == "done"


def test_pr_close_unmerged_webhook_auto_fails_task(client):
    """PR closed without merge must directly set task state to 'failed'."""
    test_client, db_url = client
    insert_guild(db_url, "gd-close-1")
    _set_webhook_secret(db_url, "gd-close-1", "secret-c1")
    _insert_task_with_worker(
        db_url,
        task_id="t-close-1",
        guild_id="gd-close-1",
        pr_url="https://github.com/o/r/pull/11",
        pr_number=11,
        pr_repo="o/r",
    )
    payload = _pr_payload(
        action="closed", repo="o/r", number=11, pull_request_extra={"merged": False}
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-c1", body, event="pull_request", delivery="d-close-1")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-close-1", content=body, headers=headers)
    assert resp.status_code == 202
    assert _get_task_state(db_url, "t-close-1") == "failed"


def test_pr_merge_skips_already_terminal_task(client):
    """Auto-finalize on PR merge must not overwrite a task already in a terminal state."""
    test_client, db_url = client
    insert_guild(db_url, "gd-term-1")
    _set_webhook_secret(db_url, "gd-term-1", "secret-t1")
    insert_worker(db_url, "gd-term-1", "w-term-1", state="online")
    insert_task(
        db_url,
        "gd-term-1",
        "t-term-1",
        worker_id="w-term-1",
        state="cancelled",
        pr_url="https://github.com/o/r/pull/12",
        pr_number=12,
        pr_repo="o/r",
    )
    payload = _pr_payload(
        action="closed", repo="o/r", number=12, pull_request_extra={"merged": True}
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-t1", body, event="pull_request", delivery="d-term-1")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-term-1", content=body, headers=headers)
    assert resp.status_code == 202
    # State must remain cancelled — not overwritten to done
    assert _get_task_state(db_url, "t-term-1") == "cancelled"


def test_webhook_skips_foreman_when_no_task_match(client):
    test_client, db_url = client
    insert_guild(db_url, "gd2")
    _set_webhook_secret(db_url, "gd2", "ssecret")
    payload = _pr_payload(action="opened", repo="o/r", number=99)
    body = json.dumps(payload).encode()
    headers = _signed_headers("ssecret", body, event="pull_request", delivery="d-disp-2")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd2", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 0


def test_webhook_skips_foreman_for_bot_on_non_ci(client):
    test_client, db_url = client
    insert_guild(db_url, "gd3")
    _set_webhook_secret(db_url, "gd3", "ssecret")
    _insert_task_with_worker(
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
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd3", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 0


def test_webhook_dispatches_for_ci_bot_check_run(client):
    test_client, db_url = client
    insert_guild(db_url, "gd4")
    _set_webhook_secret(db_url, "gd4", "ssecret")
    _insert_task_with_worker(
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
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd4", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 1
    key_arg, guild_arg, summary_arg, _user_arg = mock_q.schedule.call_args.args
    assert "gd4" in key_arg
    assert guild_arg == "gd4"
    assert "rspec" in summary_arg


# ---------------------------------------------------------------------------
# Debounce behaviour
# ---------------------------------------------------------------------------


class TestDebounce:
    """Per-PR debounce timer: rapid events coalesce; separated events deliver separately."""

    @asynccontextmanager
    async def _patched_env(self, wh):
        """Async context manager: fresh DebounceQueue instance + captured foreman calls.

        Each call creates a brand-new DebounceQueue so tests cannot bleed state
        into or out of the module-level singleton.  The queue is shut down on
        exit so any in-flight timers are cancelled and awaited cleanly.
        """
        self._foreman_calls: list[tuple[str, str, str | None, str | None]] = []

        async def fake_run_foreman(guild_id, summary, *, user_id=None, task_id=None, trigger=None):
            self._foreman_calls.append((guild_id, summary, user_id, task_id))

        queue = wh.DebounceQueue(window_seconds=0.05)
        with (
            patch.object(wh, "_debounce_queue", queue),
            patch.object(wh, "run_foreman_ai", new=fake_run_foreman),
            patch.object(wh, "ensure_poll_loop"),
        ):
            try:
                yield
            finally:
                await queue.shutdown()

    async def test_rapid_events_single_foreman_call(self):
        """Three events within the 0.05 s window → one combined foreman invocation."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            await wh._debounce_queue.schedule("g-rapid:t-r1", "g-rapid", "event A", "u1")
            await wh._debounce_queue.schedule("g-rapid:t-r1", "g-rapid", "event B", "u1")
            await wh._debounce_queue.schedule("g-rapid:t-r1", "g-rapid", "event C", "u1")
            await asyncio.sleep(0.2)

        assert len(self._foreman_calls) == 1, f"expected 1 call, got {self._foreman_calls}"
        _, summary, _, _ = self._foreman_calls[0]
        assert "event A" in summary
        assert "event B" in summary
        assert "event C" in summary

    async def test_slow_events_separate_foreman_calls(self):
        """Events separated by > debounce window → two independent foreman calls."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            await wh._debounce_queue.schedule("g-slow:t-s1", "g-slow", "event X", "u2")
            await asyncio.sleep(0.2)  # first timer fires
            await wh._debounce_queue.schedule("g-slow:t-s1", "g-slow", "event Y", "u2")
            await asyncio.sleep(0.2)  # second timer fires

        assert len(self._foreman_calls) == 2
        assert "event X" in self._foreman_calls[0][1]
        assert "event Y" in self._foreman_calls[1][1]

    async def test_reset_cancels_previous_timer(self):
        """New event before timer fires cancels the old timer (no early delivery)."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            await wh._debounce_queue.schedule("g-cancel:t-c1", "g-cancel", "first", "u3")
            await asyncio.sleep(0.02)  # less than the 0.05 s window
            await wh._debounce_queue.schedule("g-cancel:t-c1", "g-cancel", "second", "u3")
            await asyncio.sleep(0.2)  # let the reset timer fire

        # Only one delivery; the first timer was cancelled before it could fire
        assert len(self._foreman_calls) == 1
        _, summary, _, _ = self._foreman_calls[0]
        assert "first" in summary
        assert "second" in summary

    async def test_independent_prs_not_merged(self):
        """Events on different PR keys have independent timers and fire separately."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            await wh._debounce_queue.schedule("g-ind:t-pr1", "g-ind", "pr1 event", "u4")
            await wh._debounce_queue.schedule("g-ind:t-pr2", "g-ind", "pr2 event", "u4")
            await asyncio.sleep(0.2)

        assert len(self._foreman_calls) == 2
        summaries = {c[1] for c in self._foreman_calls}
        assert any("pr1 event" in s for s in summaries)
        assert any("pr2 event" in s for s in summaries)

    async def test_state_isolation_between_tests(self):
        """Each _patched_env provides a fresh DebounceQueue with no state from prior tests."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            assert wh._debounce_queue._buffers == {}
            assert wh._debounce_queue._tasks == {}
            assert wh._debounce_queue._generation == {}

    async def test_shutdown_cancels_pending_timers(self):
        """shutdown() cancels in-flight timers and clears all state without delivering."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            await wh._debounce_queue.schedule("g-sd:t-sd1", "g-sd", "pending event", "u6")
            await wh._debounce_queue.shutdown()
            assert len(self._foreman_calls) == 0
            assert wh._debounce_queue._buffers == {}
            assert wh._debounce_queue._tasks == {}
            assert wh._debounce_queue._generation == {}

    async def test_stale_generation_does_not_deliver(self):
        """_fire coroutine with stale generation must not deliver even if sleep completes."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            q = wh._debounce_queue
            key = "g-stalegen:t-sg1"

            # State as if a newer timer (gen=2) has taken over
            q._buffers[key] = [("stale event", "u8", None)]
            q._generation[key] = 2

            # Run _fire with the old gen (1) — stale coroutine waking up
            await q._fire(key, "g-stalegen", 1)

            assert len(self._foreman_calls) == 0
            assert q._buffers.get(key) == [("stale event", "u8", None)]

    async def test_cancelled_events_not_lost(self):
        """Events buffered before an external cancellation are preserved for re-delivery."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            await wh._debounce_queue.schedule("g-cl:t-cl1", "g-cl", "first event", "u5")
            await wh._debounce_queue.schedule("g-cl:t-cl1", "g-cl", "second event", "u5")

            # Externally cancel the pending timer
            task = wh._debounce_queue._tasks.get("g-cl:t-cl1")
            assert task is not None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            # Buffer must still hold both events
            assert len(wh._debounce_queue._buffers.get("g-cl:t-cl1", [])) == 2

            # Scheduling a third event re-uses the preserved buffer and delivers all three
            await wh._debounce_queue.schedule("g-cl:t-cl1", "g-cl", "third event", "u5")
            await asyncio.sleep(0.2)

        assert len(self._foreman_calls) == 1
        _, summary, _, _ = self._foreman_calls[0]
        assert "first event" in summary
        assert "second event" in summary
        assert "third event" in summary

    async def test_generation_counter_increments_on_each_reset(self):
        """Each schedule() call increments the generation; external cancel does not."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            key = "g-gci:t-gci1"
            assert wh._debounce_queue._generation.get(key) is None

            await wh._debounce_queue.schedule(key, "g-gci", "event 1", "u9")
            assert wh._debounce_queue._generation[key] == 1

            await wh._debounce_queue.schedule(key, "g-gci", "event 2", "u9")
            assert wh._debounce_queue._generation[key] == 2

            await wh._debounce_queue.schedule(key, "g-gci", "event 3", "u9")
            assert wh._debounce_queue._generation[key] == 3

            # External cancel must NOT change the generation
            task = wh._debounce_queue._tasks[key]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert wh._debounce_queue._generation[key] == 3

            await asyncio.sleep(0.2)  # nothing fires — timer was externally cancelled

        assert len(self._foreman_calls) == 0

    async def test_external_cancel_preserves_buffer_for_redelivery(self):
        """External cancellation leaves generation + buffer intact for the next schedule()."""
        import routes.webhooks as wh

        async with self._patched_env(wh):
            key = "g-ecr:t-ecr1"

            await wh._debounce_queue.schedule(key, "g-ecr", "event A", "u10")
            assert wh._debounce_queue._generation[key] == 1

            task = wh._debounce_queue._tasks[key]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            assert wh._debounce_queue._generation[key] == 1
            assert len(wh._debounce_queue._buffers.get(key, [])) == 1

            await wh._debounce_queue.schedule(key, "g-ecr", "event B", "u10")
            assert wh._debounce_queue._generation[key] == 2
            await asyncio.sleep(0.2)

        assert len(self._foreman_calls) == 1
        _, summary, _, _ = self._foreman_calls[0]
        assert "event A" in summary
        assert "event B" in summary

    async def test_race_condition_no_events_dropped(self):
        """Rapid schedule() cancellation must not silently drop events.

        Sequence: schedule A (gen=1), yield, schedule B (gen=2, cancels timer-1),
        let timer-2 fire → both A and B must be delivered together.
        """
        import routes.webhooks as wh

        async with self._patched_env(wh):
            key = "g-race:t-rc1"
            await wh._debounce_queue.schedule(key, "g-race", "event A", "u-race")
            for _ in range(3):
                await asyncio.sleep(0)
            await wh._debounce_queue.schedule(key, "g-race", "event B", "u-race")
            await asyncio.sleep(0.2)

        assert len(self._foreman_calls) == 1, (
            f"expected exactly one delivery, got {len(self._foreman_calls)}"
        )
        _, summary, _, _ = self._foreman_calls[0]
        assert "event A" in summary
        assert "event B" in summary


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


# ---------------------------------------------------------------------------
# review_requested: summary builder
# ---------------------------------------------------------------------------


class TestBuildSummaryReviewRequested:
    def test_review_requested_summary_contains_key_fields(self):
        payload = {
            "pull_request": {
                "title": "Add feature X",
                "html_url": "https://github.com/o/r/pull/5",
            },
            "requested_reviewer": {"login": "guild-bot"},
        }
        s = _build_foreman_summary(
            "pull_request", "review_requested", payload, "o/r", 5, "", "alice"
        )
        assert "[github-event] pull_request/review_requested on o/r#5 (new)" in s
        assert "@guild-bot" in s
        assert "Add feature X" in s
        assert "--approve" in s
        assert "Do NOT commit" in s
        assert "duplicates" in s

    def test_review_requested_with_task_id_shows_task(self):
        payload = {
            "pull_request": {"title": "Fix bug", "html_url": "https://github.com/o/r/pull/7"},
            "requested_reviewer": {"login": "reviewer"},
        }
        s = _build_foreman_summary(
            "pull_request", "review_requested", payload, "o/r", 7, "t-existing", "alice"
        )
        assert "(task t-existing)" in s

    def test_review_requested_header_with_empty_task_id(self):
        payload = {
            "pull_request": {"title": "T", "html_url": ""},
            "requested_reviewer": {"login": "r"},
        }
        s = _build_foreman_summary("pull_request", "review_requested", payload, "o/r", 1, "", None)
        assert "(new)" in s
        assert "(task )" not in s


# ---------------------------------------------------------------------------
# review_requested: end-to-end webhook → foreman dispatch
# ---------------------------------------------------------------------------


def _set_guild_owner_token(db_url: str, user_id: str, username: str) -> None:
    """Ensure a GithubToken row exists for user_id with the given username."""
    make_auth_token(db_url, user_id=user_id, username=username)


def test_review_requested_dispatches_when_reviewer_matches_guild_owner(client):
    """review_requested targeted at the guild owner must dispatch to the foreman."""
    test_client, db_url = client
    insert_guild(db_url, "gd-rr-1", owner_user_id="u-rr-owner")
    _set_guild_owner_token(db_url, "u-rr-owner", "guild-bot")
    _set_webhook_secret(db_url, "gd-rr-1", "secret-rr-1")

    payload = {
        "action": "review_requested",
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 20,
            "html_url": "https://github.com/o/r/pull/20",
            "title": "Add feature X",
        },
        "requested_reviewer": {"login": "guild-bot"},
        "sender": {"login": "alice"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-rr-1", body, event="pull_request", delivery="d-rr-1")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-rr-1", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 1
    key_arg, guild_arg, summary_arg, _user_arg = mock_q.schedule.call_args.args
    assert "gd-rr-1" in key_arg
    assert "review_requested" in key_arg
    assert guild_arg == "gd-rr-1"
    assert "guild-bot" in summary_arg
    assert "--approve" in summary_arg


def test_review_requested_skipped_when_reviewer_does_not_match(client):
    """review_requested for a different reviewer must not dispatch to the foreman."""
    test_client, db_url = client
    insert_guild(db_url, "gd-rr-2", owner_user_id="u-rr-owner2")
    _set_guild_owner_token(db_url, "u-rr-owner2", "guild-bot")
    _set_webhook_secret(db_url, "gd-rr-2", "secret-rr-2")

    payload = {
        "action": "review_requested",
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 21,
            "html_url": "https://github.com/o/r/pull/21",
            "title": "Fix",
        },
        "requested_reviewer": {"login": "other-person"},
        "sender": {"login": "alice"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-rr-2", body, event="pull_request", delivery="d-rr-2")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-rr-2", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 0


def test_review_requested_skipped_when_no_owner_token(client):
    """review_requested must not dispatch if the guild owner has no GitHub token."""
    test_client, db_url = client
    # insert_guild creates the owner user but no GithubToken
    insert_guild(db_url, "gd-rr-3", owner_user_id="u-rr-notoken")
    _set_webhook_secret(db_url, "gd-rr-3", "secret-rr-3")

    payload = {
        "action": "review_requested",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 22, "html_url": "https://github.com/o/r/pull/22", "title": "T"},
        "requested_reviewer": {"login": "u-rr-notoken"},
        "sender": {"login": "alice"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-rr-3", body, event="pull_request", delivery="d-rr-3")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-rr-3", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 0


def test_review_requested_reviewer_matching_is_case_insensitive(client):
    """Reviewer login comparison must be case-insensitive."""
    test_client, db_url = client
    insert_guild(db_url, "gd-rr-4", owner_user_id="u-rr-case")
    _set_guild_owner_token(db_url, "u-rr-case", "Guild-Bot")
    _set_webhook_secret(db_url, "gd-rr-4", "secret-rr-4")

    payload = {
        "action": "review_requested",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 23, "html_url": "https://github.com/o/r/pull/23", "title": "T"},
        "requested_reviewer": {"login": "guild-bot"},  # lowercase vs stored "Guild-Bot"
        "sender": {"login": "alice"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-rr-4", body, event="pull_request", delivery="d-rr-4")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-rr-4", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 1


def test_review_requested_duplicate_delivery_is_idempotent(client):
    """Re-delivery of the same webhook (same delivery_id) must not dispatch twice."""
    test_client, db_url = client
    insert_guild(db_url, "gd-rr-5", owner_user_id="u-rr-dup")
    _set_guild_owner_token(db_url, "u-rr-dup", "guild-bot")
    _set_webhook_secret(db_url, "gd-rr-5", "secret-rr-5")

    payload = {
        "action": "review_requested",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 24, "html_url": "https://github.com/o/r/pull/24", "title": "T"},
        "requested_reviewer": {"login": "guild-bot"},
        "sender": {"login": "alice"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-rr-5", body, event="pull_request", delivery="d-rr-5-dup")

    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp1 = test_client.post("/webhooks/github/gd-rr-5", content=body, headers=headers)
        resp2 = test_client.post("/webhooks/github/gd-rr-5", content=body, headers=headers)

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    # Second delivery is idempotent — only one dispatch
    assert mock_q.schedule.call_count == 1


# ---------------------------------------------------------------------------
# review_requested: cooldown (repo, PR number, head sha)
# ---------------------------------------------------------------------------


def test_review_requested_second_request_within_cooldown_is_skipped(client):
    """A second review_requested for the same PR/sha within the cooldown window
    must not dispatch a second review task."""
    test_client, db_url = client
    insert_guild(db_url, "gd-rr-cd1", owner_user_id="u-rr-cd1")
    _set_guild_owner_token(db_url, "u-rr-cd1", "guild-bot")
    _set_webhook_secret(db_url, "gd-rr-cd1", "secret-rr-cd1")

    payload = {
        "action": "review_requested",
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 30,
            "html_url": "https://github.com/o/r/pull/30",
            "title": "T",
            "head": {"ref": "feature-x", "sha": "sha-cd-1"},
        },
        "requested_reviewer": {"login": "guild-bot"},
        "sender": {"login": "alice"},
    }
    body1 = json.dumps(payload).encode()
    headers1 = _signed_headers("secret-rr-cd1", body1, event="pull_request", delivery="d-rr-cd1-a")
    # Different delivery id (e.g. a re-request), same PR/sha payload.
    headers2 = _signed_headers("secret-rr-cd1", body1, event="pull_request", delivery="d-rr-cd1-b")

    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp1 = test_client.post("/webhooks/github/gd-rr-cd1", content=body1, headers=headers1)
        resp2 = test_client.post("/webhooks/github/gd-rr-cd1", content=body1, headers=headers2)

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    # Cooldown suppresses the second dispatch even though delivery ids differ.
    assert mock_q.schedule.call_count == 1


def test_review_requested_new_head_sha_bypasses_cooldown(client):
    """A genuinely new commit (different head sha) must be reviewed immediately,
    even if the previous sha on the same PR is still within its cooldown."""
    test_client, db_url = client
    insert_guild(db_url, "gd-rr-cd2", owner_user_id="u-rr-cd2")
    _set_guild_owner_token(db_url, "u-rr-cd2", "guild-bot")
    _set_webhook_secret(db_url, "gd-rr-cd2", "secret-rr-cd2")

    def _payload(sha: str) -> dict:
        return {
            "action": "review_requested",
            "repository": {"full_name": "o/r"},
            "pull_request": {
                "number": 31,
                "html_url": "https://github.com/o/r/pull/31",
                "title": "T",
                "head": {"ref": "feature-y", "sha": sha},
            },
            "requested_reviewer": {"login": "guild-bot"},
            "sender": {"login": "alice"},
        }

    body1 = json.dumps(_payload("sha-cd-2a")).encode()
    body2 = json.dumps(_payload("sha-cd-2b")).encode()
    headers1 = _signed_headers("secret-rr-cd2", body1, event="pull_request", delivery="d-rr-cd2-a")
    headers2 = _signed_headers("secret-rr-cd2", body2, event="pull_request", delivery="d-rr-cd2-b")

    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp1 = test_client.post("/webhooks/github/gd-rr-cd2", content=body1, headers=headers1)
        resp2 = test_client.post("/webhooks/github/gd-rr-cd2", content=body2, headers=headers2)

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    # Different head sha means a new cooldown key — both dispatch.
    assert mock_q.schedule.call_count == 2


def test_review_requested_cooldown_expires_after_configured_window(client):
    """Once PR_REVIEW_COOLDOWN_SECONDS elapses, a repeat request dispatches again."""
    test_client, db_url = client
    insert_guild(db_url, "gd-rr-cd3", owner_user_id="u-rr-cd3")
    _set_guild_owner_token(db_url, "u-rr-cd3", "guild-bot")
    _set_webhook_secret(db_url, "gd-rr-cd3", "secret-rr-cd3")

    payload = {
        "action": "review_requested",
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 32,
            "html_url": "https://github.com/o/r/pull/32",
            "title": "T",
            "head": {"ref": "feature-z", "sha": "sha-cd-3"},
        },
        "requested_reviewer": {"login": "guild-bot"},
        "sender": {"login": "alice"},
    }
    body = json.dumps(payload).encode()
    headers1 = _signed_headers("secret-rr-cd3", body, event="pull_request", delivery="d-rr-cd3-a")
    headers2 = _signed_headers("secret-rr-cd3", body, event="pull_request", delivery="d-rr-cd3-b")

    with (
        patch("routes.webhooks._debounce_queue") as mock_q,
        patch.dict(os.environ, {"PR_REVIEW_COOLDOWN_SECONDS": "0"}),
    ):
        mock_q.schedule = AsyncMock()
        resp1 = test_client.post("/webhooks/github/gd-rr-cd3", content=body, headers=headers1)
        resp2 = test_client.post("/webhooks/github/gd-rr-cd3", content=body, headers=headers2)

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    # Cooldown of 0 seconds is already expired by the time the second request lands.
    assert mock_q.schedule.call_count == 2


# ---------------------------------------------------------------------------
# _get_guild_owner_github_login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_guild_owner_github_login_returns_username(db_session):
    """_get_guild_owner_github_login must return the guild owner's GitHub username."""
    insert_guild(db_session, "g-owner-login", owner_user_id="u-owner-x")
    make_auth_token(db_session, user_id="u-owner-x", username="owner-login-x")

    engine = create_async_engine(db_session, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        from models import Guild
        from sqlmodel import col, select

        guild_pk = (
            await session.exec(select(col(Guild.id)).where(col(Guild.slug) == "g-owner-login"))
        ).one()
        login = await _get_guild_owner_github_login(session, guild_pk)

    assert login == "owner-login-x"
    await engine.dispose()


@pytest.mark.asyncio
async def test_get_guild_owner_github_login_returns_none_without_token(db_session):
    """_get_guild_owner_github_login returns None when no GithubToken exists for the owner."""
    insert_guild(db_session, "g-owner-notoken", owner_user_id="u-notoken")

    engine = create_async_engine(db_session, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        from models import Guild
        from sqlmodel import col, select

        guild_pk = (
            await session.exec(select(col(Guild.id)).where(col(Guild.slug) == "g-owner-notoken"))
        ).one()
        login = await _get_guild_owner_github_login(session, guild_pk)

    assert login is None
    await engine.dispose()


# ---------------------------------------------------------------------------
# Prompt: review_requested section
# ---------------------------------------------------------------------------


def test_prompt_describes_review_requested_behavior():
    """System prompt must document the review_requested auto-dispatch behavior."""
    from foreman.prompt import FOREMAN_SYSTEM

    assert "review_requested" in FOREMAN_SYSTEM
    assert "--approve" in FOREMAN_SYSTEM
    assert "Do NOT commit" in FOREMAN_SYSTEM
    assert "duplicates" in FOREMAN_SYSTEM


# ---------------------------------------------------------------------------
# devReady issue pickup: _devready_issue_trigger
# ---------------------------------------------------------------------------


class TestDevReadyIssueTrigger:
    def _issue_payload(self, *, labels: list[str], number: int = 1) -> dict:
        return {"issue": {"number": number, "labels": [{"name": name} for name in labels]}}

    def test_labeled_devready_triggers(self):
        payload = {"issue": {"number": 1}, "label": {"name": "devReady"}}
        assert _devready_issue_trigger("issues", "labeled", payload) == "devReady"

    def test_labeled_non_devready_does_not_trigger(self):
        payload = {"issue": {"number": 1}, "label": {"name": "bug"}}
        assert _devready_issue_trigger("issues", "labeled", payload) is None

    def test_labeled_case_insensitive(self):
        payload = {"issue": {"number": 1}, "label": {"name": "DEV-READY"}}
        assert _devready_issue_trigger("issues", "labeled", payload) == "DEV-READY"

    def test_labeled_all_aliases(self):
        for alias in ("devReady", "dev-ready", "ready-for-dev", "ready"):
            payload = {"issue": {"number": 1}, "label": {"name": alias}}
            assert _devready_issue_trigger("issues", "labeled", payload) == alias

    def test_opened_with_devready_label_triggers(self):
        payload = self._issue_payload(labels=["enhancement", "ready-for-dev"])
        assert _devready_issue_trigger("issues", "opened", payload) == "ready-for-dev"

    def test_opened_without_devready_label_does_not_trigger(self):
        payload = self._issue_payload(labels=["enhancement"])
        assert _devready_issue_trigger("issues", "opened", payload) is None

    def test_reopened_with_devready_label_triggers(self):
        payload = self._issue_payload(labels=["devReady"])
        assert _devready_issue_trigger("issues", "reopened", payload) == "devReady"

    def test_other_actions_do_not_trigger(self):
        payload = self._issue_payload(labels=["devReady"])
        assert _devready_issue_trigger("issues", "closed", payload) is None
        assert _devready_issue_trigger("issues", "edited", payload) is None
        assert _devready_issue_trigger("issues", "unlabeled", payload) is None

    def test_non_issues_event_does_not_trigger(self):
        payload = {"issue": {"number": 1}, "label": {"name": "devReady"}}
        assert _devready_issue_trigger("issue_comment", "labeled", payload) is None

    def test_missing_issue_object_does_not_trigger(self):
        assert _devready_issue_trigger("issues", "opened", {}) is None


# ---------------------------------------------------------------------------
# devReady issue pickup: summary builder
# ---------------------------------------------------------------------------


class TestBuildSummaryDevReadyIssue:
    def test_labeled_summary_contains_key_fields(self):
        payload = {
            "issue": {
                "number": 42,
                "title": "Add dark mode",
                "html_url": "https://github.com/o/r/issues/42",
                "assignees": [],
            },
            "label": {"name": "devReady"},
        }
        s = _build_foreman_summary("issues", "labeled", payload, "o/r", 42, "", None)
        assert "[github-event] issues/labeled on o/r#42 (new)" in s
        assert "Label applied: devReady" in s
        assert "Add dark mode" in s
        assert "claim_github_issue" in s
        assert "devReady pickup flow" in s

    def test_opened_with_label_summary(self):
        payload = {
            "issue": {
                "number": 7,
                "title": "Fix flaky test",
                "html_url": "https://github.com/o/r/issues/7",
                "labels": [{"name": "ready"}],
                "assignees": [{"login": "alice"}],
            },
        }
        s = _build_foreman_summary("issues", "opened", payload, "o/r", 7, "", "alice")
        assert "[github-event] issues/opened on o/r#7 (new)" in s
        assert "Label applied: ready" in s
        assert "Current assignees: alice" in s


# ---------------------------------------------------------------------------
# devReady issue pickup: end-to-end webhook -> foreman dispatch
# ---------------------------------------------------------------------------


def _issues_payload(*, action: str, repo: str, number: int, labels: list[str], **extra) -> dict:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "issue": {
            "number": number,
            "title": extra.pop("title", "Some issue"),
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "labels": [{"name": name} for name in labels],
            "assignees": extra.pop("assignees", []),
        },
        "sender": {"login": extra.pop("sender_login", "octocat")},
        **({"label": {"name": extra.pop("label_name")}} if "label_name" in extra else {}),
        **extra,
    }


def test_issues_labeled_devready_dispatches_to_foreman(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-dr-1")
    _set_webhook_secret(db_url, "gd-dr-1", "secret-dr-1")

    payload = _issues_payload(
        action="labeled", repo="o/r", number=100, labels=["devReady"], label_name="devReady"
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-dr-1", body, event="issues", delivery="d-dr-1")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-dr-1", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 1
    key_arg, guild_arg, summary_arg, _user_arg = mock_q.schedule.call_args.args
    assert "gd-dr-1" in key_arg
    assert "issues-devready" in key_arg
    assert "o/r#100" in key_arg
    assert guild_arg == "gd-dr-1"
    assert "devReady" in summary_arg
    assert "claim_github_issue" in summary_arg


def test_issues_labeled_non_devready_does_not_dispatch(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-dr-2")
    _set_webhook_secret(db_url, "gd-dr-2", "secret-dr-2")

    payload = _issues_payload(
        action="labeled", repo="o/r", number=101, labels=["bug"], label_name="bug"
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-dr-2", body, event="issues", delivery="d-dr-2")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-dr-2", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 0


def test_issues_opened_with_devready_label_dispatches(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-dr-3")
    _set_webhook_secret(db_url, "gd-dr-3", "secret-dr-3")

    payload = _issues_payload(
        action="opened", repo="o/r", number=102, labels=["dev-ready", "enhancement"]
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-dr-3", body, event="issues", delivery="d-dr-3")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-dr-3", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 1
    key_arg, *_ = mock_q.schedule.call_args.args
    assert "o/r#102" in key_arg


def test_issues_opened_without_devready_label_does_not_dispatch(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-dr-4")
    _set_webhook_secret(db_url, "gd-dr-4", "secret-dr-4")

    payload = _issues_payload(action="opened", repo="o/r", number=103, labels=["enhancement"])
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-dr-4", body, event="issues", delivery="d-dr-4")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-dr-4", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 0


def test_issues_reopened_with_devready_label_dispatches(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-dr-5")
    _set_webhook_secret(db_url, "gd-dr-5", "secret-dr-5")

    payload = _issues_payload(action="reopened", repo="o/r", number=104, labels=["ready"])
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-dr-5", body, event="issues", delivery="d-dr-5")
    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp = test_client.post("/webhooks/github/gd-dr-5", content=body, headers=headers)
    assert resp.status_code == 202
    assert mock_q.schedule.call_count == 1


def test_issues_devready_duplicate_delivery_is_idempotent(client):
    test_client, db_url = client
    insert_guild(db_url, "gd-dr-6")
    _set_webhook_secret(db_url, "gd-dr-6", "secret-dr-6")

    payload = _issues_payload(
        action="labeled", repo="o/r", number=105, labels=["devReady"], label_name="devReady"
    )
    body = json.dumps(payload).encode()
    headers = _signed_headers("secret-dr-6", body, event="issues", delivery="d-dr-6-dup")

    with patch("routes.webhooks._debounce_queue") as mock_q:
        mock_q.schedule = AsyncMock()
        resp1 = test_client.post("/webhooks/github/gd-dr-6", content=body, headers=headers)
        resp2 = test_client.post("/webhooks/github/gd-dr-6", content=body, headers=headers)

    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert mock_q.schedule.call_count == 1


# ---------------------------------------------------------------------------
# Prompt: devReady issue webhook section
# ---------------------------------------------------------------------------


def test_prompt_describes_devready_issue_webhook_behavior():
    """System prompt must document the immediate devReady webhook pickup flow."""
    from foreman.prompt import FOREMAN_SYSTEM

    assert "issues/labeled" in FOREMAN_SYSTEM
    assert "issues/opened" in FOREMAN_SYSTEM
    assert "issues/reopened" in FOREMAN_SYSTEM
    assert "claim_github_issue" in FOREMAN_SYSTEM
    assert "devReady pickup flow" in FOREMAN_SYSTEM


# ---------------------------------------------------------------------------
# Regression: _deliver must call run_foreman_ai with a valid signature (#1263)
# ---------------------------------------------------------------------------


async def test_deliver_kwargs_match_run_foreman_ai_signature():
    """_deliver's call must bind against the real run_foreman_ai signature.

    Guards against drift like the `child=` kwarg (removed in #1200) that made
    every debounced batch raise TypeError against a patched-out foreman.
    """
    import inspect

    import routes.webhooks as wh
    from foreman.runner import run_foreman_ai

    recorded: list[tuple[tuple, dict]] = []

    async def recorder(*args, **kwargs):
        recorded.append((args, kwargs))

    queue = wh.DebounceQueue(window_seconds=0.05)
    with (
        patch.object(wh, "run_foreman_ai", new=recorder),
        patch.object(wh, "ensure_poll_loop"),
    ):
        await queue._deliver("g-sig:t-sig1", "g-sig", [("event", "u-sig", "t-sig1")])

    assert len(recorded) == 1
    args, kwargs = recorded[0]
    # Raises TypeError if _deliver passes an unknown or misspelled kwarg.
    inspect.signature(run_foreman_ai).bind(*args, **kwargs)
