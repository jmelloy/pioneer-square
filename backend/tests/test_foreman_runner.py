"""Tests for foreman tool-result message structure (issue #87).

The Anthropic API requires tool results to be a user turn containing a
tool_result content block — not a plain text user message.  These tests
verify that exec_tools always returns the correct structure regardless of
success or failure, and that tool_use_id always matches the input id.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


def _mock_tool_use(name: str, id_: str, input_: dict) -> MagicMock:
    tu = MagicMock()
    tu.name = name
    tu.id = id_
    tu.input = input_
    return tu


def _mock_session() -> AsyncMock:
    """Return an AsyncMock that quacks like an AsyncSession."""
    session = AsyncMock()
    session.close = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()
    result = AsyncMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    result.one_or_none = MagicMock(return_value=None)
    result.scalars = MagicMock(return_value=AsyncMock())
    session.execute = AsyncMock(return_value=result)
    return session


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------


def test_exec_tools_result_has_required_keys():
    """Every result block must contain type, tool_use_id, and a string content."""
    from foreman.tools import exec_tools

    tu = _mock_tool_use("message_worker", "toolu_abc123", {"worker_id": "w-1", "message": "hello"})

    with (
        patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())),
        patch("foreman.tools.broadcast", new=AsyncMock()),
        patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
    ):
        results = _run(exec_tools("guild1", [tu]))

    assert len(results) == 1
    r = results[0]
    assert r["type"] == "tool_result"
    assert r["tool_use_id"] == "toolu_abc123"
    assert isinstance(r["content"], str)


def test_exec_tools_tool_use_id_matches_input():
    """tool_use_id in the result must exactly match the id on the tool_use block."""
    from foreman.tools import exec_tools

    specific_id = "toolu_specific_xyz_999"
    tu = _mock_tool_use("message_worker", specific_id, {"worker_id": "w-1", "message": "ping"})

    with (
        patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())),
        patch("foreman.tools.broadcast", new=AsyncMock()),
        patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
    ):
        results = _run(exec_tools("guild1", [tu]))

    assert results[0]["tool_use_id"] == specific_id


def test_exec_tools_content_is_always_string():
    """result content must be a string even for tools that return structured data."""
    from foreman.tools import exec_tools

    session = _mock_session()
    # get_task_status path: task not found → result_text = "Task t-xxx not found."
    session.execute.return_value.scalar_one_or_none.return_value = None

    tu = _mock_tool_use("get_task_status", "toolu_str_check", {"task_id": "t-notexist"})

    with (
        patch("foreman.tools.get_db", AsyncMock(return_value=session)),
        patch("foreman.tools.broadcast", new=AsyncMock()),
    ):
        results = _run(exec_tools("guild1", [tu]))

    assert isinstance(results[0]["content"], str)


def test_exec_tools_no_is_error_on_success():
    """Successful results must NOT include is_error (or have it False)."""
    from foreman.tools import exec_tools

    tu = _mock_tool_use("message_worker", "toolu_ok", {"worker_id": "w-1", "message": "hi"})

    with (
        patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())),
        patch("foreman.tools.broadcast", new=AsyncMock()),
        patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
    ):
        results = _run(exec_tools("guild1", [tu]))

    r = results[0]
    assert not r.get("is_error"), "successful tool result must not set is_error"


# ---------------------------------------------------------------------------
# Error-handling tests
# ---------------------------------------------------------------------------


def test_exec_tools_error_sets_is_error_true():
    """When a tool raises an unexpected exception, the result must set is_error: True."""
    from foreman.tools import exec_tools

    tu = _mock_tool_use("finalize_task", "toolu_err1", {"task_id": "t-bad"})

    with patch("foreman.tools.get_db", AsyncMock(side_effect=RuntimeError("DB unavailable"))):
        results = _run(exec_tools("guild1", [tu]))

    assert len(results) == 1
    r = results[0]
    assert r["type"] == "tool_result"
    assert r["tool_use_id"] == "toolu_err1"
    assert r.get("is_error") is True
    assert isinstance(r["content"], str)
    assert r["content"]  # non-empty error message


def test_exec_tools_error_preserves_tool_use_id():
    """Even on failure, tool_use_id must match the original tool_use block id."""
    from foreman.tools import exec_tools

    error_id = "toolu_preserve_id_on_error"
    tu = _mock_tool_use("create_task", error_id, {"name": "boom", "description": "x"})

    with patch("foreman.tools.get_db", AsyncMock(side_effect=Exception("kaboom"))):
        results = _run(exec_tools("guild1", [tu]))

    assert results[0]["tool_use_id"] == error_id


def test_exec_tools_multiple_tools_all_returned():
    """With multiple tool_use blocks, all must produce result entries in order."""
    from foreman.tools import exec_tools

    ids = ["toolu_first", "toolu_second", "toolu_third"]
    tus = [
        _mock_tool_use("message_worker", id_, {"worker_id": "w-1", "message": "m"}) for id_ in ids
    ]

    with (
        patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())),
        patch("foreman.tools.broadcast", new=AsyncMock()),
        patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
    ):
        results = _run(exec_tools("guild1", tus))

    assert len(results) == 3
    for i, r in enumerate(results):
        assert r["tool_use_id"] == ids[i]
        assert r["type"] == "tool_result"


def test_exec_tools_partial_failure_still_returns_all():
    """If one tool in a batch fails, remaining tools are still executed and returned."""
    from foreman.tools import exec_tools

    session = _mock_session()
    call_count = 0

    async def get_db_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("first tool DB error")
        return session

    tus = [
        _mock_tool_use("finalize_task", "toolu_fail", {"task_id": "t-bad"}),
        _mock_tool_use("message_worker", "toolu_ok", {"worker_id": "w-1", "message": "ok"}),
    ]

    with (
        patch("foreman.tools.get_db", side_effect=get_db_side_effect),
        patch("foreman.tools.broadcast", new=AsyncMock()),
        patch("foreman.tools.emit_terminal_line", new=AsyncMock()),
    ):
        results = _run(exec_tools("guild1", tus))

    assert len(results) == 2
    assert results[0]["tool_use_id"] == "toolu_fail"
    assert results[0].get("is_error") is True
    assert results[1]["tool_use_id"] == "toolu_ok"
    assert not results[1].get("is_error")


# ---------------------------------------------------------------------------
# GitHub error tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cross-context task lock tests (issue #927)
#
# Parent (whole-guild) runs and per-task child runs used to key their
# `_guild_locks` serialisation entries differently — (guild_id, user_id) for
# parents vs. (guild_id, "task:<id>") for children — so the two never
# serialised against each other even when targeting the very same task. These
# tests exercise the gate added to send_followup/redirect_task/cancel_task/
# finalize_task that checks the other context's lock before mutating.
# ---------------------------------------------------------------------------


def test_is_child_task_run_active_reflects_guild_locks_state():
    """is_child_task_run_active must track the same (guild, "task:<id>") lock
    entries run_foreman_ai uses to serialise per-task child runs."""
    from foreman.runner import _guild_locks, _GuildRunLock, is_child_task_run_active

    guild_id = "g-lockcheck"
    task_id = "t-lockcheck1"
    key = (guild_id, f"task:{task_id}")

    assert is_child_task_run_active(guild_id, task_id) is False

    _guild_locks[key] = _GuildRunLock(busy=True)
    try:
        assert is_child_task_run_active(guild_id, task_id) is True
        _guild_locks[key].busy = False
        assert is_child_task_run_active(guild_id, task_id) is False
    finally:
        _guild_locks.pop(key, None)


def _with_child_lock_busy(guild_id: str, task_id: str):
    """Context manager marking (guild_id, "task:<task_id>") busy in _guild_locks,
    mimicking an in-flight per-task child Foreman run."""
    from contextlib import contextmanager

    from foreman.runner import _guild_locks, _GuildRunLock

    @contextmanager
    def _cm():
        key = (guild_id, f"task:{task_id}")
        _guild_locks[key] = _GuildRunLock(busy=True)
        try:
            yield
        finally:
            _guild_locks.pop(key, None)

    return _cm()


@pytest.mark.parametrize(
    "tool_name,extra_input",
    [
        ("send_followup", {"instructions": "fix ci"}),
        ("redirect_task", {"instructions": "do something else"}),
        ("cancel_task", {"reason": "no longer needed"}),
        ("finalize_task", {}),
    ],
)
def test_parent_run_blocked_when_child_run_active(tool_name, extra_input):
    """A parent-context tool call (own_task_id=None) targeting a task whose
    own child run currently holds the lock must be skipped, not executed."""
    from foreman.tools import exec_tools

    guild_id = "g-parent-vs-child"
    task_id = "t-race1"
    tu = _mock_tool_use(tool_name, f"toolu_{tool_name}", {"task_id": task_id, **extra_input})

    with _with_child_lock_busy(guild_id, task_id):
        with patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())):
            results = _run(exec_tools(guild_id, [tu], own_task_id=None))

    r = results[0]
    assert r.get("is_error") is True
    assert "in-flight child" in r["content"]


@pytest.mark.parametrize(
    "tool_name,extra_input",
    [
        ("send_followup", {"instructions": "fix ci"}),
        ("redirect_task", {"instructions": "do something else"}),
        ("cancel_task", {"reason": "no longer needed"}),
        ("finalize_task", {}),
    ],
)
def test_child_run_not_blocked_by_its_own_lock(tool_name, extra_input):
    """A child run mutating its own task must never be blocked by the very
    lock entry it holds — own_task_id == task_id must skip the gate."""
    from foreman.tools import exec_tools

    guild_id = "g-child-self"
    task_id = "t-self1"
    tu = _mock_tool_use(tool_name, f"toolu_{tool_name}", {"task_id": task_id, **extra_input})

    with _with_child_lock_busy(guild_id, task_id):
        with patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())):
            results = _run(exec_tools(guild_id, [tu], own_task_id=task_id))

    r = results[0]
    assert "in-flight child" not in r["content"]


def test_parent_run_not_blocked_when_no_child_run_active():
    """Baseline: with no competing lock held, the parent gate is a no-op."""
    from foreman.tools import exec_tools

    guild_id = "g-no-race"
    task_id = "t-norace1"
    tu = _mock_tool_use("cancel_task", "toolu_norace", {"task_id": task_id})

    with patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())):
        results = _run(exec_tools(guild_id, [tu], own_task_id=None))

    r = results[0]
    assert "in-flight child" not in r["content"]


def test_exec_tools_github_http_error_sets_is_error():
    """GitHub HTTPError must produce is_error: True, not a plain success result."""
    import urllib.error

    from foreman.tools import exec_tools

    tu = _mock_tool_use("list_github_issues", "toolu_gh_err", {"repo": "owner/repo"})

    http_err = urllib.error.HTTPError(
        url="https://api.github.com/repos/owner/repo/issues",
        code=403,
        msg="Forbidden",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )

    with (
        patch("foreman.tools.get_db", AsyncMock(return_value=_mock_session())),
        patch("foreman.tools._guild_github_token", AsyncMock(return_value=("tok", "user"))),
        patch("foreman.tools._gh_api", side_effect=http_err),
    ):
        results = _run(exec_tools("guild1", [tu]))

    r = results[0]
    assert r["type"] == "tool_result"
    assert r["tool_use_id"] == "toolu_gh_err"
    assert r.get("is_error") is True
    assert "403" in r["content"] or "Forbidden" in r["content"]
