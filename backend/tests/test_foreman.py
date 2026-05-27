"""Comprehensive tests for the foreman subsystem.

Covers:
1. Tool-call dispatching — each tool routes to the correct handler
2. Tool-call result handling — serialisation, error capture, edge cases
3. Foreman prompt building — dynamic context, primary_repo behaviour
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL  # noqa: E402
from foreman.prompt import FOREMAN_SYSTEM, build_system_prompt
from foreman.runner import (
    _fetch_online_workers,  # noqa: E402
    _load_history,
    _save_turn,
)
from foreman.tools import exec_tools
from foreman_core.constants import MAX_HISTORY_MESSAGES, MAX_TOOL_RESULT_CHARS
from foreman_core.message_utils import (
    _serialize_content,
    _summarize_task,
    prune_history,
    truncate_tool_result,
)
from helpers import _sync_session, create_db, insert_agent, insert_guild, insert_task, insert_worker
from models import Guild, Lock, Task, TaskEvent, TaskLog  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlmodel import col  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database for foreman tests.

    Truncates all tables before each test for isolation. The schema is
    already set up by the session-scoped _setup_schema fixture in conftest.py.
    """
    from helpers import truncate_all

    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", db_url)

    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield db_url


def _fake_tool_use(name: str, inputs: dict, tool_id: str = "tool-abc123") -> SimpleNamespace:
    """Build a minimal tool-use object that exec_tools expects."""
    return SimpleNamespace(name=name, input=inputs, id=tool_id)


def _insert_worker(db_url: str, guild_id: str, worker_id: str) -> None:
    insert_worker(db_url, guild_id, worker_id, state="idle")


def _insert_task(
    db_url: str,
    task_id: str,
    guild_id: str,
    worker_id: str,
    state: str = "awaiting-review",
    phase: str = "execute",
    issue_number: int | None = None,
    issue_repo: str | None = None,
    branch: str | None = "claude/test-branch-abc123",
) -> None:
    insert_task(
        db_url,
        guild_id,
        task_id,
        worker_id=worker_id,
        description="do the thing",
        state=state,
        phase=phase,
        tool="claude",
        branch=branch,
        issue_number=issue_number,
        issue_repo=issue_repo,
    )


def _insert_agent(
    db_url: str,
    guild_id: str,
    worker_id: str,
    agent_id: str = "a-test01",
    state: str = "idle",
) -> None:
    """Add a worker-attached agent (defaults to idle) so send_followup has a target."""
    insert_agent(db_url, guild_id, agent_id, worker_id=worker_id, state=state)


# ---------------------------------------------------------------------------
# 1. Prompt building
# ---------------------------------------------------------------------------


class TestBuildSystemPrompt:
    def test_contains_base_system_text(self):
        prompt = build_system_prompt("[]", "[]")
        assert "Foreman AI" in prompt
        assert "Pioneer Square" in prompt

    def test_contains_workers_block(self):
        workers = '[{"id": "w-abc", "state": "idle"}]'
        prompt = build_system_prompt(workers, "[]")
        assert workers in prompt
        assert "## Current workers" in prompt

    def test_contains_tasks_block(self):
        tasks = '[{"id": "t-xyz", "state": "done"}]'
        prompt = build_system_prompt("[]", tasks)
        assert tasks in prompt
        assert "## Recent tasks" in prompt

    def test_extra_context_included(self):
        prompt = build_system_prompt("[]", "[]", extra_context="Task t-001 just completed.")
        assert "## Context" in prompt
        assert "Task t-001 just completed." in prompt

    def test_extra_context_omitted_when_empty(self):
        prompt = build_system_prompt("[]", "[]", extra_context="")
        assert "## Context" not in prompt

    def test_primary_repo_included_when_set(self):
        prompt = build_system_prompt("[]", "[]", primary_repo="acme/backend")
        assert "acme/backend" in prompt
        assert "Check it first" in prompt

    def test_primary_repo_omitted_when_none(self):
        prompt = build_system_prompt("[]", "[]", primary_repo=None)
        assert "Check it first" not in prompt

    def test_primary_repo_omitted_when_empty_string(self):
        prompt = build_system_prompt("[]", "[]", primary_repo="")
        assert "Check it first" not in prompt

    def test_primary_repo_line_appears_before_workers(self):
        prompt = build_system_prompt("[]", "[]", primary_repo="org/repo")
        repo_pos = prompt.index("org/repo")
        workers_pos = prompt.index("## Current workers")
        assert repo_pos < workers_pos

    def test_prompt_structure_without_primary_repo(self):
        prompt = build_system_prompt('["w1"]', '["t1"]')
        assert prompt.startswith(FOREMAN_SYSTEM)

    def test_empty_workers_renders_no_workers_message(self):
        prompt = build_system_prompt("[]", "[]")
        assert "## Current workers" in prompt
        assert "No workers are currently online" in prompt
        # Should not render an empty JSON code block when there are no workers
        assert "```json\n[]\n```" not in prompt.split("## Recent tasks", 1)[0]

    def test_pretty_printed_empty_workers_renders_no_workers_message(self):
        # json.dumps([], indent=2) produces "[]" but be defensive about whitespace
        prompt = build_system_prompt("[\n]", "[]")
        assert "No workers are currently online" in prompt

    def test_non_empty_workers_renders_json_block(self):
        workers = '[{"id": "w-abc", "state": "online"}]'
        prompt = build_system_prompt(workers, "[]")
        assert workers in prompt
        assert "No workers are currently online" not in prompt


# ---------------------------------------------------------------------------
# 1b. _fetch_online_workers (filters workers by state=='online')
# ---------------------------------------------------------------------------


def _insert_worker_with_state(db_url: str, guild_id: str, worker_id: str, state: str) -> None:
    insert_worker(db_url, guild_id, worker_id, state=state)


class TestFetchOnlineWorkers:
    @pytest.mark.asyncio
    async def test_excludes_offline_workers(self, db_session):
        from foreman.runner import _fetch_online_workers

        guild_id = "g-fetch1"
        insert_guild(db_session, guild_id)
        _insert_worker_with_state(db_session, guild_id, "w-online1", "online")
        _insert_worker_with_state(db_session, guild_id, "w-offline1", "offline")
        _insert_worker_with_state(db_session, guild_id, "w-idle1", "idle")

        async with database_module.AsyncSessionLocal() as db:
            rows = await _fetch_online_workers(db, guild_id)

        ids = {row["id"] for row in rows}
        assert ids == {"w-online1"}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_online_workers(self, db_session):
        from foreman.runner import _fetch_online_workers

        guild_id = "g-fetch2"
        insert_guild(db_session, guild_id)
        _insert_worker_with_state(db_session, guild_id, "w-offline2", "offline")

        async with database_module.AsyncSessionLocal() as db:
            rows = await _fetch_online_workers(db, guild_id)

        assert rows == []

    @pytest.mark.asyncio
    async def test_scopes_to_guild(self, db_session):
        from foreman.runner import _fetch_online_workers

        guild_a = "g-fetch3a"
        guild_b = "g-fetch3b"
        insert_guild(db_session, guild_a)
        insert_guild(db_session, guild_b)
        _insert_worker_with_state(db_session, guild_a, "w-a", "online")
        _insert_worker_with_state(db_session, guild_b, "w-b", "online")

        async with database_module.AsyncSessionLocal() as db:
            rows = await _fetch_online_workers(db, guild_a)

        assert {row["id"] for row in rows} == {"w-a"}


# ---------------------------------------------------------------------------
# 2. _serialize_content (runner helper)
# ---------------------------------------------------------------------------


class TestSerializeContent:
    def test_string_input(self):
        result = _serialize_content("hello")
        assert json.loads(result) == "hello"

    def test_list_of_dicts(self):
        blocks = [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "x"}]
        result = _serialize_content(blocks)
        assert json.loads(result) == blocks

    def test_list_with_sdk_object_model_dump(self):
        obj = MagicMock()
        obj.model_dump.return_value = {"type": "text", "text": "sdk block"}
        result = _serialize_content([obj])
        parsed = json.loads(result)
        assert parsed == [{"type": "text", "text": "sdk block"}]

    def test_list_with_object_lacking_model_dump(self):
        obj = SimpleNamespace(type="tool_use")
        result = _serialize_content([obj])
        parsed = json.loads(result)
        assert parsed[0]["type"] == "tool_use"

    def test_non_string_non_list_falls_back_to_str(self):
        result = _serialize_content(42)
        assert json.loads(result) == "42"

    def test_empty_list(self):
        result = _serialize_content([])
        assert json.loads(result) == []


# ---------------------------------------------------------------------------
# 3. Tool dispatching — correct handler invoked, result format
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db_session")
class TestExecToolsDispatching:
    """Each tool name must route to the correct handler and return a tool_result block."""

    async def test_create_task_returns_task_id(self, db_session):
        insert_guild(db_session, "g-create")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-create",
                [_fake_tool_use("create_task", {"name": "My Task", "description": "Do something"})],
            )
        assert len(results) == 1
        r = results[0]
        assert r["type"] == "tool_result"
        assert r["tool_use_id"] == "tool-abc123"
        assert "t-" in r["content"]
        assert "My Task" in r["content"]

    async def test_create_task_default_phase_is_execute(self, db_session):
        insert_guild(db_session, "g-phase")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-phase", [_fake_tool_use("create_task", {"name": "Task", "description": "Work"})]
            )
        # Task row should have phase=execute (not specified → default)
        task_id = results[0]["content"].split()[1]

        with _sync_session(db_session) as session:
            phase = session.scalar(select(col(Task.phase)).where(col(Task.id) == task_id))
        assert phase == "execute"

    async def test_create_task_stamps_user_id(self, db_session):
        """Tasks created via the foreman remember which user initiated them so
        worker callbacks can later route the conversation back to the right
        user thread instead of always falling through to the guild owner."""
        insert_guild(db_session, "g-stamp")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-stamp",
                [_fake_tool_use("create_task", {"name": "Owned", "description": "task"})],
                user_id="gh-user-42",
            )
        task_id = results[0]["content"].split()[1]

        with _sync_session(db_session) as session:
            user_id = session.scalar(select(col(Task.user_id)).where(col(Task.id) == task_id))
        assert user_id == "gh-user-42"

    async def test_assign_task_new_stamps_user_id(self, db_session):
        insert_guild(db_session, "g-stamp-assign")
        _insert_worker(db_session, "g-stamp-assign", "w-stamp1")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-stamp-assign",
                [
                    _fake_tool_use(
                        "assign_task",
                        {"worker_id": "w-stamp1", "description": "do it"},
                    )
                ],
                user_id="gh-user-99",
            )
        # exec_tools returns "Task t-XXXXXX queued for w-stamp1." — extract the id.
        content = results[0]["content"]
        task_id = next(tok for tok in content.split() if tok.startswith("t-"))

        with _sync_session(db_session) as session:
            user_id = session.scalar(select(col(Task.user_id)).where(col(Task.id) == task_id))
        assert user_id == "gh-user-99"

    async def test_create_task_custom_phase(self, db_session):
        insert_guild(db_session, "g-planphase")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-planphase",
                [
                    _fake_tool_use(
                        "create_task",
                        {"name": "Plan task", "description": "Plan it", "phase": "plan"},
                    )
                ],
            )
        task_id = results[0]["content"].split()[1]

        with _sync_session(db_session) as session:
            phase = session.scalar(select(col(Task.phase)).where(col(Task.id) == task_id))
        assert phase == "plan"

    async def test_assign_task_unknown_worker(self, db_session):
        insert_guild(db_session, "g-assign-bad")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-assign-bad",
                [
                    _fake_tool_use(
                        "assign_task", {"worker_id": "w-nosuch", "description": "Do work"}
                    )
                ],
            )
        assert "not found" in results[0]["content"].lower()

    async def test_assign_task_creates_new_task(self, db_session):
        insert_guild(db_session, "g-assign-new")
        _insert_worker(db_session, "g-assign-new", "w-worker1")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-assign-new",
                [
                    _fake_tool_use(
                        "assign_task",
                        {
                            "worker_id": "w-worker1",
                            "description": "Write tests",
                            "name": "Test task",
                        },
                    )
                ],
            )
        assert "queued" in results[0]["content"].lower()
        assert "w-worker1" in results[0]["content"]

    async def test_assign_task_with_existing_task_id(self, db_session):
        insert_guild(db_session, "g-assign-existing")
        _insert_worker(db_session, "g-assign-existing", "w-wkr2")
        _insert_task(db_session, "t-exist1", "g-assign-existing", "w-wkr2", state="pending")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-assign-existing",
                [
                    _fake_tool_use(
                        "assign_task",
                        {
                            "worker_id": "w-wkr2",
                            "description": "Updated description",
                            "task_id": "t-exist1",
                        },
                    )
                ],
            )
        assert "assigned" in results[0]["content"].lower()
        assert "t-exist1" in results[0]["content"]

    async def test_send_followup_task_not_found(self, db_session):
        insert_guild(db_session, "g-followup-missing")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-followup-missing",
                [
                    _fake_tool_use(
                        "send_followup", {"task_id": "t-nosuch", "instructions": "Fix it"}
                    )
                ],
            )
        assert "not found" in results[0]["content"].lower()

    async def test_send_followup_broadcasts_and_returns_message(self, db_session):
        insert_guild(db_session, "g-followup-ok")
        _insert_worker(db_session, "g-followup-ok", "w-flwup")
        _insert_agent(db_session, "g-followup-ok", "w-flwup", "a-flwup1")
        _insert_task(db_session, "t-flwup1", "g-followup-ok", "w-flwup")
        broadcast_calls = []

        async def capture_broadcast(gid, msg):
            broadcast_calls.append(msg)

        with patch("foreman.tools.broadcast", side_effect=capture_broadcast):
            results = await exec_tools(
                "g-followup-ok",
                [
                    _fake_tool_use(
                        "send_followup", {"task_id": "t-flwup1", "instructions": "Add more tests"}
                    )
                ],
            )
        assert "t-flwup1" in results[0]["content"]
        followup_msgs = [m for m in broadcast_calls if m.get("type") == "task-followup"]
        assert len(followup_msgs) == 1
        assert followup_msgs[0]["instructions"] == "Add more tests"
        assert followup_msgs[0]["branch"] == "claude/test-branch-abc123"
        assert followup_msgs[0]["workerId"] == "w-flwup"

    async def test_send_followup_falls_back_to_any_idle_worker(self, db_session):
        """When the original worker has no idle agent, send_followup picks
        any other idle worker in the guild (and updates task.worker_id)."""
        insert_guild(db_session, "g-followup-fallback")
        _insert_worker(db_session, "g-followup-fallback", "w-orig")
        _insert_worker(db_session, "g-followup-fallback", "w-other")
        # Original worker has only a busy agent; the fallback worker is idle.
        _insert_agent(db_session, "g-followup-fallback", "w-orig", "a-orig", state="working")
        _insert_agent(db_session, "g-followup-fallback", "w-other", "a-other", state="idle")
        _insert_task(db_session, "t-flwup-fb", "g-followup-fallback", "w-orig")
        broadcast_calls = []

        async def capture(gid, msg):
            broadcast_calls.append(msg)

        with patch("foreman.tools.broadcast", side_effect=capture):
            results = await exec_tools(
                "g-followup-fallback",
                [
                    _fake_tool_use(
                        "send_followup",
                        {"task_id": "t-flwup-fb", "instructions": "Continue"},
                    )
                ],
            )
        followup_msgs = [m for m in broadcast_calls if m.get("type") == "task-followup"]
        assert len(followup_msgs) == 1
        assert followup_msgs[0]["workerId"] == "w-other"
        assert "reassigned" in results[0]["content"].lower()

        with _sync_session(db_session) as session:
            wid = session.scalar(select(col(Task.worker_id)).where(col(Task.id) == "t-flwup-fb"))
        assert wid == "w-other"

    async def test_send_followup_errors_when_no_idle_worker(self, db_session):
        insert_guild(db_session, "g-followup-noidle")
        _insert_worker(db_session, "g-followup-noidle", "w-busy")
        _insert_agent(db_session, "g-followup-noidle", "w-busy", "a-busy", state="working")
        _insert_task(db_session, "t-noidle", "g-followup-noidle", "w-busy")
        broadcast_calls = []

        async def capture(gid, msg):
            broadcast_calls.append(msg)

        with patch("foreman.tools.broadcast", side_effect=capture):
            results = await exec_tools(
                "g-followup-noidle",
                [
                    _fake_tool_use(
                        "send_followup",
                        {"task_id": "t-noidle", "instructions": "go"},
                    )
                ],
            )
        assert "no idle worker" in results[0]["content"].lower()
        assert results[0].get("is_error") is True
        followup_msgs = [m for m in broadcast_calls if m.get("type") == "task-followup"]
        assert len(followup_msgs) == 0

    async def test_finalize_task_not_found(self, db_session):
        insert_guild(db_session, "g-finalize-missing")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-finalize-missing", [_fake_tool_use("finalize_task", {"task_id": "t-nosuch"})]
            )
        assert "not found" in results[0]["content"].lower()

    async def test_finalize_task_marks_done(self, db_session):
        insert_guild(db_session, "g-finalize-ok")
        _insert_worker(db_session, "g-finalize-ok", "w-fin")
        _insert_task(db_session, "t-fin1", "g-finalize-ok", "w-fin")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-finalize-ok", [_fake_tool_use("finalize_task", {"task_id": "t-fin1"})]
            )
        assert "finalized" in results[0]["content"].lower()

        with _sync_session(db_session) as session:
            state = session.scalar(select(col(Task.state)).where(col(Task.id) == "t-fin1"))
        assert state == "done"

    async def test_message_worker_dispatches(self, db_session):
        insert_guild(db_session, "g-msgwkr")
        _insert_worker(db_session, "g-msgwkr", "w-msgwkr")
        broadcast_calls = []

        async def capture(gid, msg):
            broadcast_calls.append(msg)

        with (
            patch("foreman.tools.broadcast", side_effect=capture),
            patch("foreman.tools.emit_terminal_line", new_callable=AsyncMock),
        ):
            results = await exec_tools(
                "g-msgwkr",
                [
                    _fake_tool_use(
                        "message_worker", {"worker_id": "w-msgwkr", "message": "Hello worker"}
                    )
                ],
            )
        assert "delivered" in results[0]["content"].lower()
        worker_msgs = [m for m in broadcast_calls if m.get("type") == "worker-message"]
        assert len(worker_msgs) == 1
        assert worker_msgs[0]["message"] == "Hello worker"

    async def test_redirect_task_not_found(self, db_session):
        insert_guild(db_session, "g-redirect-missing")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-redirect-missing",
                [
                    _fake_tool_use(
                        "redirect_task", {"task_id": "t-nosuch", "instructions": "Go different way"}
                    )
                ],
            )
        assert "not found" in results[0]["content"].lower()

    async def test_redirect_task_already_done(self, db_session):
        insert_guild(db_session, "g-redirect-done")
        _insert_worker(db_session, "g-redirect-done", "w-redir")
        _insert_task(db_session, "t-done1", "g-redirect-done", "w-redir", state="done")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-redirect-done",
                [
                    _fake_tool_use(
                        "redirect_task", {"task_id": "t-done1", "instructions": "Try again"}
                    )
                ],
            )
        assert "cannot redirect" in results[0]["content"].lower()

    async def test_redirect_task_working(self, db_session):
        insert_guild(db_session, "g-redirect-ok")
        _insert_worker(db_session, "g-redirect-ok", "w-redir2")
        _insert_task(db_session, "t-wk1", "g-redirect-ok", "w-redir2", state="working")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-redirect-ok",
                [
                    _fake_tool_use(
                        "redirect_task", {"task_id": "t-wk1", "instructions": "New direction"}
                    )
                ],
            )
        assert "redirect" in results[0]["content"].lower()

    async def test_cancel_task_not_found(self, db_session):
        insert_guild(db_session, "g-cancel-missing")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-cancel-missing", [_fake_tool_use("cancel_task", {"task_id": "t-nosuch"})]
            )
        assert "not found" in results[0]["content"].lower()

    async def test_cancel_task_already_done(self, db_session):
        insert_guild(db_session, "g-cancel-done")
        _insert_worker(db_session, "g-cancel-done", "w-cancel")
        _insert_task(db_session, "t-cdone", "g-cancel-done", "w-cancel", state="done")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-cancel-done", [_fake_tool_use("cancel_task", {"task_id": "t-cdone"})]
            )
        assert "already" in results[0]["content"].lower()

    async def test_cancel_task_pending(self, db_session):
        insert_guild(db_session, "g-cancel-ok")
        _insert_worker(db_session, "g-cancel-ok", "w-cancel2")
        _insert_task(db_session, "t-cpend", "g-cancel-ok", "w-cancel2", state="pending")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-cancel-ok",
                [
                    _fake_tool_use(
                        "cancel_task", {"task_id": "t-cpend", "reason": "No longer needed"}
                    )
                ],
            )
        assert "cancelled" in results[0]["content"].lower()
        assert "No longer needed" in results[0]["content"]

        with _sync_session(db_session) as session:
            state = session.scalar(select(col(Task.state)).where(col(Task.id) == "t-cpend"))
        assert state == "cancelled"

    async def test_shutdown_worker_not_found(self, db_session):
        insert_guild(db_session, "g-sd-missing")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-sd-missing",
                [_fake_tool_use("shutdown_worker", {"worker_id": "w-nosuch"})],
            )
        assert "not found" in results[0]["content"].lower()

    async def test_shutdown_worker_broadcasts_signal(self, db_session):
        insert_guild(db_session, "g-sd-ok")
        _insert_worker(db_session, "g-sd-ok", "w-sd")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock) as mock_bcast:
            results = await exec_tools(
                "g-sd-ok",
                [
                    _fake_tool_use(
                        "shutdown_worker",
                        {"worker_id": "w-sd", "reason": "winding down"},
                    )
                ],
            )
        assert "shutdown signal sent" in results[0]["content"].lower()
        assert "winding down" in results[0]["content"]
        # The handler must broadcast a worker-shutdown message targeting the worker.
        shutdown_calls = [
            c for c in mock_bcast.await_args_list if c.args[1].get("type") == "worker-shutdown"
        ]
        assert len(shutdown_calls) == 1
        payload = shutdown_calls[0].args[1]
        assert payload["workerId"] == "w-sd"
        assert payload.get("reason") == "winding down"

    async def test_shutdown_worker_omits_reason_when_blank(self, db_session):
        insert_guild(db_session, "g-sd-noreason")
        _insert_worker(db_session, "g-sd-noreason", "w-sd2")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock) as mock_bcast:
            await exec_tools(
                "g-sd-noreason",
                [_fake_tool_use("shutdown_worker", {"worker_id": "w-sd2"})],
            )
        shutdown_calls = [
            c for c in mock_bcast.await_args_list if c.args[1].get("type") == "worker-shutdown"
        ]
        assert len(shutdown_calls) == 1
        assert "reason" not in shutdown_calls[0].args[1]

    async def test_get_task_status_not_found(self, db_session):
        insert_guild(db_session, "g-status-missing")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-status-missing", [_fake_tool_use("get_task_status", {"task_id": "t-nosuch"})]
            )
        assert "not found" in results[0]["content"].lower()

    async def test_get_task_status_returns_json(self, db_session):
        insert_guild(db_session, "g-status-ok")
        _insert_worker(db_session, "g-status-ok", "w-status")
        _insert_task(db_session, "t-stat1", "g-status-ok", "w-status", state="working")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-status-ok", [_fake_tool_use("get_task_status", {"task_id": "t-stat1"})]
            )
        data = json.loads(results[0]["content"])
        assert data["id"] == "t-stat1"
        assert data["state"] == "working"
        assert "recent_logs" in data

    async def test_unknown_tool_name_returns_empty_result(self, db_session):
        """An unknown tool name should not raise; result content may be empty."""
        insert_guild(db_session, "g-unknown")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-unknown", [_fake_tool_use("totally_unknown_tool", {"foo": "bar"})]
            )
        assert len(results) == 1
        assert results[0]["type"] == "tool_result"

    async def test_tool_result_structure(self, db_session):
        """Every result block must have type, tool_use_id, and content keys."""
        insert_guild(db_session, "g-struct")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-struct",
                [_fake_tool_use("finalize_task", {"task_id": "t-nosuch"}, tool_id="tid-99")],
            )
        r = results[0]
        assert r["type"] == "tool_result"
        assert r["tool_use_id"] == "tid-99"
        assert "content" in r

    async def test_multiple_tools_in_one_call(self, db_session):
        """Multiple tool uses are all executed and all results returned."""
        insert_guild(db_session, "g-multi")
        _insert_worker(db_session, "g-multi", "w-multi")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-multi",
                [
                    _fake_tool_use("create_task", {"name": "A", "description": "Work A"}, "id-1"),
                    _fake_tool_use("create_task", {"name": "B", "description": "Work B"}, "id-2"),
                ],
            )
        assert len(results) == 2
        assert results[0]["tool_use_id"] == "id-1"
        assert results[1]["tool_use_id"] == "id-2"

    async def test_tool_input_args_passed_through(self, db_session):
        """Worker ID, description, and phase from inputs must appear in the DB row."""
        insert_guild(db_session, "g-inputs")
        _insert_worker(db_session, "g-inputs", "w-inputcheck")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            await exec_tools(
                "g-inputs",
                [
                    _fake_tool_use(
                        "assign_task",
                        {
                            "worker_id": "w-inputcheck",
                            "description": "Specific description text",
                            "name": "Specific name",
                            "phase": "review",
                            "tool": "codex",
                        },
                    )
                ],
            )

        with _sync_session(db_session) as session:
            row = session.execute(
                select(Task).where(col(Task.worker_id) == "w-inputcheck")
            ).scalar_one_or_none()
        assert row is not None
        assert row.description == "Specific description text"
        assert row.name == "Specific name"
        assert row.phase == "review"
        assert row.tool == "codex"

    # -----------------------------------------------------------------------
    # Task locking — prevent concurrent follow-up races
    # -----------------------------------------------------------------------

    async def test_send_followup_acquires_lock(self, db_session):
        """Successful send_followup creates a row in the locks table for the task."""
        insert_guild(db_session, "g-lock-set")
        _insert_worker(db_session, "g-lock-set", "w-lock1")
        _insert_agent(db_session, "g-lock-set", "w-lock1", "a-lock1")
        _insert_task(db_session, "t-lock1", "g-lock-set", "w-lock1")

        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-lock-set",
                [
                    _fake_tool_use(
                        "send_followup", {"task_id": "t-lock1", "instructions": "Fix tests"}
                    )
                ],
            )
        assert "t-lock1" in results[0]["content"]

        with _sync_session(db_session) as session:
            lock = session.execute(
                select(Lock).where(col(Lock.key) == "task:t-lock1")
            ).scalar_one_or_none()
        assert lock is not None, "A lock row should exist in the locks table after dispatch"
        assert lock.owner is not None, "Lock owner should be set after dispatch"

    async def test_send_followup_while_locked_queues_event(self, db_session):
        """A second send_followup on a locked task queues the instructions instead of dispatching."""
        insert_guild(db_session, "g-lock-queue")
        _insert_worker(db_session, "g-lock-queue", "w-lq1")
        _insert_agent(db_session, "g-lock-queue", "w-lq1", "a-lq1")
        # Pre-lock the task to simulate a concurrent dispatch already in flight.
        _insert_task(db_session, "t-lq1", "g-lock-queue", "w-lq1")

        now_dt = datetime.now(UTC)
        expires_dt = datetime.now(UTC) + timedelta(minutes=30)
        with _sync_session(db_session) as session:
            session.add(
                Lock(
                    key="task:t-lq1",
                    owner="existing-holder",
                    acquired_at=now_dt,
                    expires_at=expires_dt,
                )
            )
            session.commit()

        broadcast_calls = []

        async def capture(gid, msg):
            broadcast_calls.append(msg)

        with patch("foreman.tools.broadcast", side_effect=capture):
            results = await exec_tools(
                "g-lock-queue",
                [
                    _fake_tool_use(
                        "send_followup",
                        {"task_id": "t-lq1", "instructions": "Add integration tests"},
                    )
                ],
            )

        # No task-followup broadcast — we didn't dispatch
        followup_msgs = [m for m in broadcast_calls if m.get("type") == "task-followup"]
        assert len(followup_msgs) == 0
        # The result should mention queued / locked
        assert (
            "queued" in results[0]["content"].lower() or "locked" in results[0]["content"].lower()
        )
        # A task_event row should exist

        with _sync_session(db_session) as session:
            ev = session.execute(
                select(TaskEvent).where(col(TaskEvent.task_id) == "t-lq1")
            ).scalar_one_or_none()
        assert ev is not None, "A task_event row should have been inserted"
        assert ev.event_type == "pending-followup"
        payload = json.loads(ev.payload_json)
        assert payload["instructions"] == "Add integration tests"

    async def test_two_concurrent_followups_only_one_dispatched(self, db_session):
        """Simulate two simultaneous send_followup calls — exactly one dispatches, one queues."""
        insert_guild(db_session, "g-race")
        _insert_worker(db_session, "g-race", "w-race1")
        _insert_agent(db_session, "g-race", "w-race1", "a-race1")
        _insert_task(db_session, "t-race1", "g-race", "w-race1")

        broadcast_calls = []

        async def capture(gid, msg):
            broadcast_calls.append(msg)

        with patch("foreman.tools.broadcast", side_effect=capture):
            # Fire both concurrently, matching what two async foreman runs would do.
            results = await asyncio.gather(
                exec_tools(
                    "g-race",
                    [
                        _fake_tool_use(
                            "send_followup",
                            {"task_id": "t-race1", "instructions": "Fix lint"},
                            "tid-1",
                        )
                    ],
                ),
                exec_tools(
                    "g-race",
                    [
                        _fake_tool_use(
                            "send_followup",
                            {"task_id": "t-race1", "instructions": "Fix tests"},
                            "tid-2",
                        )
                    ],
                ),
            )

        all_results = results[0] + results[1]
        followup_msgs = [m for m in broadcast_calls if m.get("type") == "task-followup"]
        # Exactly one task-followup must have been broadcast
        assert len(followup_msgs) == 1

        # Exactly one result should indicate queued/locked; the other is success
        queued = [
            r
            for r in all_results
            if "queued" in r["content"].lower() or "locked" in r["content"].lower()
        ]
        dispatched = [
            r
            for r in all_results
            if "follow-up sent" in r["content"].lower() or "reassigned" in r["content"].lower()
        ]
        assert len(queued) == 1, f"Expected 1 queued, got: {[r['content'] for r in queued]}"
        assert len(dispatched) == 1, (
            f"Expected 1 dispatched, got: {[r['content'] for r in dispatched]}"
        )

        # One task_event row should exist with the queued instructions

        with _sync_session(db_session) as session:
            rows = (
                session.execute(select(TaskEvent).where(col(TaskEvent.task_id) == "t-race1"))
                .scalars()
                .all()
            )
        assert len(rows) == 1

    async def test_finalize_task_clears_lock_and_queued_events(self, db_session):
        """finalize_task releases the lock and discards any queued task_events."""
        insert_guild(db_session, "g-fin-lock")
        _insert_worker(db_session, "g-fin-lock", "w-fin-lk")
        _insert_task(db_session, "t-fin-lk", "g-fin-lock", "w-fin-lk")

        now_dt = datetime.now(UTC)
        expires_dt = datetime.now(UTC) + timedelta(minutes=30)
        now_iso = now_dt.isoformat()
        with _sync_session(db_session) as session:
            session.add(
                Lock(
                    key="task:t-fin-lk",
                    owner="h1",
                    acquired_at=now_dt,
                    expires_at=expires_dt,
                )
            )
            session.add(
                TaskEvent(
                    task_id="t-fin-lk",
                    event_type="pending-followup",
                    payload_json='{"instructions": "stale"}',
                    created_at=now_iso,
                )
            )
            session.commit()

        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-fin-lock", [_fake_tool_use("finalize_task", {"task_id": "t-fin-lk"})]
            )
        assert "finalized" in results[0]["content"].lower()

        with _sync_session(db_session) as session:
            task_state = session.scalar(select(col(Task.state)).where(col(Task.id) == "t-fin-lk"))
            event_count = session.scalar(
                select(func.count())
                .select_from(TaskEvent)
                .where(col(TaskEvent.task_id) == "t-fin-lk")
            )
            lock_key = session.scalar(select(col(Lock.key)).where(col(Lock.key) == "task:t-fin-lk"))
        assert task_state == "done"
        assert lock_key is None, "Lock should be released on finalize"
        assert event_count == 0, "Queued events should be deleted on finalize"

    async def test_stale_lock_overridden(self, db_session):
        """An expired lock (past its TTL) is evicted on the next acquire attempt."""
        insert_guild(db_session, "g-stale-lock")
        _insert_worker(db_session, "g-stale-lock", "w-stale")
        _insert_agent(db_session, "g-stale-lock", "w-stale", "a-stale")
        _insert_task(db_session, "t-stale", "g-stale-lock", "w-stale")
        # Insert a lock with an already-expired TTL (2 hours ago).

        stale_dt = datetime.now(UTC) - timedelta(hours=2)
        with _sync_session(db_session) as session:
            session.add(
                Lock(
                    key="task:t-stale",
                    owner="old-holder",
                    acquired_at=stale_dt,
                    expires_at=stale_dt,
                )
            )
            session.commit()

        broadcast_calls = []

        async def capture(gid, msg):
            broadcast_calls.append(msg)

        with patch("foreman.tools.broadcast", side_effect=capture):
            await exec_tools(
                "g-stale-lock",
                [_fake_tool_use("send_followup", {"task_id": "t-stale", "instructions": "Retry"})],
            )
        followup_msgs = [m for m in broadcast_calls if m.get("type") == "task-followup"]
        assert len(followup_msgs) == 1, "Stale lock should be overridden and follow-up dispatched"

        with _sync_session(db_session) as session:
            lock = session.execute(
                select(Lock).where(col(Lock.key) == "task:t-stale")
            ).scalar_one_or_none()
        assert lock is not None, "A new lock should have been acquired"
        assert lock.owner != "old-holder", "Lock owner should have been replaced"


# ---------------------------------------------------------------------------
# 4. Tool result handling — serialisation, error capture, edge cases
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db_session")
class TestExecToolsResultHandling:
    async def test_result_is_string(self, db_session):
        insert_guild(db_session, "g-res-str")
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-res-str", [_fake_tool_use("create_task", {"name": "X", "description": "Y"})]
            )
        assert isinstance(results[0]["content"], str)

    async def test_github_no_token_returns_error_string(self, db_session):
        """GitHub tools must return a user-visible error when no token is available."""
        insert_guild(db_session, "g-gh-notoken")
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=None),
        ):
            results = await exec_tools(
                "g-gh-notoken", [_fake_tool_use("list_github_issues", {"repo": "org/repo"})]
            )
        assert "No GitHub token" in results[0]["content"]

    async def test_github_http_error_caught(self, db_session):
        """HTTP errors from GitHub API must be caught and returned as text, not raised."""
        import urllib.error

        insert_guild(db_session, "g-gh-httperr")
        http_err = urllib.error.HTTPError(
            url="https://api.github.com/repos/x/y/issues",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", side_effect=http_err),
        ):
            results = await exec_tools(
                "g-gh-httperr", [_fake_tool_use("list_github_issues", {"repo": "x/y"})]
            )
        assert "404" in results[0]["content"]
        assert "GitHub API error" in results[0]["content"]

    async def test_github_generic_exception_caught(self, db_session):
        """Unexpected exceptions from GitHub calls must be caught, not propagated."""
        insert_guild(db_session, "g-gh-exc")
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", side_effect=RuntimeError("network down")),
        ):
            results = await exec_tools(
                "g-gh-exc", [_fake_tool_use("list_github_issues", {"repo": "x/y"})]
            )
        assert "GitHub error" in results[0]["content"]
        assert "network down" in results[0]["content"]

    async def test_list_github_issues_returns_json(self, db_session):
        insert_guild(db_session, "g-gh-issues")
        fake_issues = [
            {
                "number": 1,
                "title": "Bug A",
                "state": "open",
                "labels": [{"name": "bug"}],
                "assignees": [],
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", return_value=fake_issues),
        ):
            results = await exec_tools(
                "g-gh-issues", [_fake_tool_use("list_github_issues", {"repo": "org/repo"})]
            )
        parsed = json.loads(results[0]["content"])
        assert len(parsed) == 1
        assert parsed[0]["number"] == 1
        assert parsed[0]["title"] == "Bug A"

    async def test_list_github_issues_filters_prs(self, db_session):
        """Issues with a pull_request key must be excluded from the result."""
        insert_guild(db_session, "g-gh-pr-filter")
        fake_issues = [
            {
                "number": 1,
                "title": "Issue",
                "state": "open",
                "labels": [],
                "assignees": [],
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "number": 2,
                "title": "PR",
                "state": "open",
                "pull_request": {},
                "labels": [],
                "assignees": [],
                "created_at": "2026-01-01T00:00:00Z",
            },
        ]
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", return_value=fake_issues),
        ):
            results = await exec_tools(
                "g-gh-pr-filter", [_fake_tool_use("list_github_issues", {"repo": "org/repo"})]
            )
        parsed = json.loads(results[0]["content"])
        assert len(parsed) == 1
        assert parsed[0]["number"] == 1

    async def test_get_github_issue_returns_body_and_comments(self, db_session):
        insert_guild(db_session, "g-gh-issue-detail")
        fake_issue = {
            "number": 42,
            "title": "The bug",
            "state": "open",
            "body": "It breaks",
            "labels": [{"name": "critical"}],
        }
        fake_comments = [{"user": {"login": "alice"}, "body": "I see it too"}]
        api_responses = iter([fake_issue, fake_comments])
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", side_effect=lambda *a, **kw: next(api_responses)),
        ):
            results = await exec_tools(
                "g-gh-issue-detail",
                [_fake_tool_use("get_github_issue", {"repo": "org/repo", "issue_number": 42})],
            )
        parsed = json.loads(results[0]["content"])
        assert parsed["number"] == 42
        assert parsed["body"] == "It breaks"
        assert parsed["comments"][0]["author"] == "alice"

    async def test_list_github_prs(self, db_session):
        insert_guild(db_session, "g-gh-prs")
        fake_prs = [
            {
                "number": 7,
                "title": "feat: add X",
                "state": "open",
                "head": {"ref": "feat/add-x"},
                "draft": False,
            }
        ]
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", return_value=fake_prs),
        ):
            results = await exec_tools(
                "g-gh-prs", [_fake_tool_use("list_github_prs", {"repo": "org/repo"})]
            )
        parsed = json.loads(results[0]["content"])
        assert parsed[0]["number"] == 7
        assert parsed[0]["head"] == "feat/add-x"

    async def test_claim_github_issue(self, db_session):
        insert_guild(db_session, "g-gh-claim")
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "octouser")),
            patch("foreman.tools._gh_api_post", return_value={}),
        ):
            results = await exec_tools(
                "g-gh-claim",
                [_fake_tool_use("claim_github_issue", {"repo": "org/repo", "issue_number": 99})],
            )
        assert "99" in results[0]["content"]
        assert "octouser" in results[0]["content"]

    async def test_create_github_issue_returns_number_and_url(self, db_session):
        insert_guild(db_session, "g-gh-createissue")
        fake_response = {
            "number": 55,
            "html_url": "https://github.com/org/repo/issues/55",
            "title": "New issue",
        }
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api_post", return_value=fake_response),
        ):
            results = await exec_tools(
                "g-gh-createissue",
                [
                    _fake_tool_use(
                        "create_github_issue",
                        {"repo": "org/repo", "title": "New issue", "body": "Details here"},
                    )
                ],
            )
        parsed = json.loads(results[0]["content"])
        assert parsed["number"] == 55
        assert parsed["url"] == "https://github.com/org/repo/issues/55"

    async def test_search_github_issues_returns_items(self, db_session):
        insert_guild(db_session, "g-gh-search")
        fake_search = {
            "items": [
                {
                    "number": 3,
                    "title": "Match",
                    "state": "open",
                    "html_url": "https://github.com/org/repo/issues/3",
                    "labels": [],
                }
            ]
        }
        with (
            patch("foreman.tools.broadcast", new_callable=AsyncMock),
            patch("foreman.tools._guild_github_token", return_value=("tok", "user")),
            patch("foreman.tools._gh_api", return_value=fake_search),
        ):
            results = await exec_tools(
                "g-gh-search",
                [
                    _fake_tool_use(
                        "search_github_issues", {"repo": "org/repo", "query": "match keyword"}
                    )
                ],
            )
        parsed = json.loads(results[0]["content"])
        assert parsed[0]["number"] == 3

    async def test_empty_result_does_not_crash(self, db_session):
        """A tool that produces an empty string result is valid."""
        insert_guild(db_session, "g-empty-res")
        # unknown tool returns empty content — verify no exception
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools("g-empty-res", [_fake_tool_use("unknown_tool_xyz", {})])
        assert results[0]["content"] == ""

    async def test_large_result_is_a_string(self, db_session):
        """Even a large JSON result from get_task_status must be a plain string."""
        insert_guild(db_session, "g-large-res")
        _insert_worker(db_session, "g-large-res", "w-large")
        _insert_task(db_session, "t-large1", "g-large-res", "w-large")
        # Insert many log lines

        now = datetime.now(UTC)
        with _sync_session(db_session) as session:
            for _i in range(50):
                session.add(
                    TaskLog(
                        task_id="t-large1",
                        timestamp=now,
                        line="x" * 200,
                        worker_id="w-large",
                    )
                )
            session.commit()
        with patch("foreman.tools.broadcast", new_callable=AsyncMock):
            results = await exec_tools(
                "g-large-res",
                [_fake_tool_use("get_task_status", {"task_id": "t-large1", "log_lines": 50})],
            )
        assert isinstance(results[0]["content"], str)
        parsed = json.loads(results[0]["content"])
        assert len(parsed["recent_logs"]) == 50


# ---------------------------------------------------------------------------
# 5. Foreman history (_load_history and _save_turn)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("db_session")
class TestForemanHistory:
    async def test_load_history_empty(self, db_session):
        insert_guild(db_session, "g-hist-empty")
        msgs = await _load_history("g-hist-empty", "u-1")
        assert msgs == []

    async def test_save_and_load_single_user_turn(self, db_session):
        insert_guild(db_session, "g-hist-one")
        await _save_turn("g-hist-one", "u-1", "user", "Hello foreman")
        msgs = await _load_history("g-hist-one", "u-1")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello foreman"

    async def test_history_starts_with_user_role(self, db_session):
        """Loaded messages must always begin with a user turn."""
        insert_guild(db_session, "g-hist-start")
        await _save_turn("g-hist-start", "u-1", "assistant", "Hi")
        await _save_turn("g-hist-start", "u-1", "user", "Hello")
        msgs = await _load_history("g-hist-start", "u-1")
        assert msgs[0]["role"] == "user"

    async def test_sliding_window_keeps_last_5_human_turns(self, db_session):
        """History window keeps the last 5 non-tool-response user turns."""
        insert_guild(db_session, "g-hist-window")
        for i in range(7):
            await _save_turn("g-hist-window", "u-1", "user", f"Message {i}")
            await _save_turn("g-hist-window", "u-1", "assistant", f"Reply {i}")
        msgs = await _load_history("g-hist-window", "u-1")
        user_turns = [m for m in msgs if m["role"] == "user"]
        assert len(user_turns) <= 5

    async def test_tool_response_turns_included_with_parent(self, db_session):
        """Tool-result user turns must be included alongside the assistant turn that requested them."""
        insert_guild(db_session, "g-hist-tool")
        await _save_turn("g-hist-tool", "u-1", "user", "Do something")
        asst_id = await _save_turn(
            "g-hist-tool",
            "u-1",
            "assistant",
            [{"type": "tool_use", "id": "tu-1", "name": "create_task"}],
        )
        await _save_turn(
            "g-hist-tool",
            "u-1",
            "user",
            [{"type": "tool_result", "tool_use_id": "tu-1", "content": "ok"}],
            is_tool_response=True,
            parent_id=asst_id,
        )
        msgs = await _load_history("g-hist-tool", "u-1")
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles

    async def test_history_isolated_per_user(self, db_session):
        """Turns from different user_ids must not bleed into each other."""
        insert_guild(db_session, "g-hist-iso")
        await _save_turn("g-hist-iso", "u-alice", "user", "Alice message")
        await _save_turn("g-hist-iso", "u-bob", "user", "Bob message")
        alice_msgs = await _load_history("g-hist-iso", "u-alice")
        bob_msgs = await _load_history("g-hist-iso", "u-bob")
        assert all("Alice" in json.dumps(m) for m in alice_msgs)
        assert all("Bob" in json.dumps(m) for m in bob_msgs)
        assert not any("Bob" in json.dumps(m) for m in alice_msgs)
        assert not any("Alice" in json.dumps(m) for m in bob_msgs)

    async def test_system_turns_excluded_from_load_history(self, db_session):
        """System turns must be saved to the DB but never appear in messages returned
        by _load_history — the Anthropic API requires system as a separate top-level
        parameter, not a message in the conversation array."""
        insert_guild(db_session, "g-hist-sys")
        await _save_turn("g-hist-sys", "u-1", "system", "You are the Foreman AI.")
        await _save_turn("g-hist-sys", "u-1", "user", "Hello foreman")
        msgs = await _load_history("g-hist-sys", "u-1")
        assert all(m["role"] != "system" for m in msgs)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello foreman"

    async def test_system_turns_visible_in_get_foreman_history(self, db_session):
        """System turns excluded from the API messages list must still be retrievable
        via get_foreman_history for auditing and debugging."""
        from foreman.runner import get_foreman_history

        insert_guild(db_session, "g-hist-sys2")
        await _save_turn("g-hist-sys2", "u-1", "system", "System prompt content")
        await _save_turn("g-hist-sys2", "u-1", "user", "Human message")
        history = await get_foreman_history("g-hist-sys2", "u-1")
        # System turn is surfaced via the dedicated "system" key, not in messages[]
        assert history["system"] == "System prompt content"
        roles = [t["role"] for t in history["messages"]]
        assert "user" in roles
        assert "system" not in roles
        assert len(history["messages"]) == 1

    async def test_system_turns_not_counted_in_sliding_window(self, db_session):
        """System turns interspersed with human turns must not affect the 5-turn
        sliding window — only non-tool-response user turns count."""
        insert_guild(db_session, "g-hist-sys3")
        for i in range(7):
            await _save_turn("g-hist-sys3", "u-1", "system", f"System prompt {i}")
            await _save_turn("g-hist-sys3", "u-1", "user", f"Human message {i}")
            await _save_turn("g-hist-sys3", "u-1", "assistant", f"Reply {i}")
        msgs = await _load_history("g-hist-sys3", "u-1")
        user_turns = [m for m in msgs if m["role"] == "user"]
        assert len(user_turns) <= 5
        assert all(m["role"] != "system" for m in msgs)

    async def test_system_turn_only_returns_empty_history(self, db_session):
        """If the only saved turn is a system turn, _load_history must return []."""
        insert_guild(db_session, "g-hist-sys4")
        await _save_turn("g-hist-sys4", "u-1", "system", "Orphaned system prompt")
        msgs = await _load_history("g-hist-sys4", "u-1")
        assert msgs == []

    async def test_tool_use_and_tool_result_saved_and_loaded(self, db_session):
        """Full tool-use / tool-result round-trip: assistant turn containing a
        tool_use block paired with a user tool_result turn must survive a DB
        round-trip and come back with the correct roles and content types."""
        insert_guild(db_session, "g-hist-toolrt")
        await _save_turn("g-hist-toolrt", "u-1", "user", "Please create a task")
        asst_id = await _save_turn(
            "g-hist-toolrt",
            "u-1",
            "assistant",
            [
                {"type": "text", "text": "Sure, creating task now."},
                {"type": "tool_use", "id": "tu-42", "name": "create_task", "input": {"name": "T"}},
            ],
        )
        await _save_turn(
            "g-hist-toolrt",
            "u-1",
            "user",
            [{"type": "tool_result", "tool_use_id": "tu-42", "content": "Created t-xyz"}],
            is_tool_response=True,
            parent_id=asst_id,
        )
        msgs = await _load_history("g-hist-toolrt", "u-1")
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
        asst_content = msgs[1]["content"]
        tool_uses = [b for b in asst_content if isinstance(b, dict) and b.get("type") == "tool_use"]
        assert len(tool_uses) == 1
        assert tool_uses[0]["id"] == "tu-42"
        assert msgs[2]["role"] == "user"
        tool_results = [
            b for b in msgs[2]["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "tu-42"
        assert tool_results[0]["content"] == "Created t-xyz"


# ---------------------------------------------------------------------------
# 6. _summarize_task — issue #95
# ---------------------------------------------------------------------------


class TestSummarizeTask:
    """Terminal tasks are summarised/excluded; non-terminal tasks are kept in full."""

    _24H = 86_400

    def _now_ts(self):
        return datetime.now(UTC).timestamp()

    def _cutoff_ts(self):
        """The cutoff used in production: now minus 24 h."""
        return self._now_ts() - self._24H

    def _iso(self, delta_secs: float) -> str:
        """Return an ISO timestamp offset by *delta_secs* from now."""
        from datetime import timedelta

        dt = datetime.now(UTC) + timedelta(seconds=delta_secs)
        return dt.isoformat()

    def test_non_terminal_task_returned_unchanged(self):
        task = {"id": "t-1", "state": "working", "description": "Do work", "branch": "main"}
        result = _summarize_task(task, self._cutoff_ts())
        assert result == task

    def test_pending_task_returned_unchanged(self):
        task = {"id": "t-2", "state": "pending", "description": "Pending work"}
        result = _summarize_task(task, self._cutoff_ts())
        assert result == task

    def test_awaiting_review_task_returned_unchanged(self):
        task = {"id": "t-3", "state": "awaiting-review", "description": "Review me"}
        result = _summarize_task(task, self._cutoff_ts())
        assert result == task

    def test_done_task_within_24h_strips_description(self):
        task = {
            "id": "t-4",
            "state": "done",
            "description": "Implement OAuth",
            "branch": "claude/feat",
            "pr_url": "https://github.com/x/y/pull/42",
            "finished_at": self._iso(-3600),  # 1 hour ago
        }
        result = _summarize_task(task, self._cutoff_ts())
        assert result is not None
        assert "description" not in result
        assert result["id"] == "t-4"
        assert result["state"] == "done"
        assert result["branch"] == "claude/feat"

    def test_failed_task_within_24h_strips_description(self):
        task = {
            "id": "t-5",
            "state": "failed",
            "description": "Big description text",
            "finished_at": self._iso(-1800),  # 30 min ago
        }
        result = _summarize_task(task, self._cutoff_ts())
        assert result is not None
        assert "description" not in result

    def test_cancelled_task_within_24h_strips_description(self):
        task = {
            "id": "t-6",
            "state": "cancelled",
            "description": "Cancelled work",
            "finished_at": self._iso(-7200),  # 2 hours ago
        }
        result = _summarize_task(task, self._cutoff_ts())
        assert result is not None
        assert "description" not in result

    def test_done_task_older_than_24h_excluded(self):
        task = {
            "id": "t-7",
            "state": "done",
            "description": "Old work",
            "finished_at": self._iso(-(self._24H + 3600)),  # 25 hours ago
        }
        result = _summarize_task(task, self._cutoff_ts())
        assert result is None

    def test_failed_task_older_than_24h_excluded(self):
        task = {
            "id": "t-8",
            "state": "failed",
            "description": "Old fail",
            "finished_at": self._iso(-(self._24H * 2)),  # 48 hours ago
        }
        result = _summarize_task(task, self._cutoff_ts())
        assert result is None

    def test_terminal_task_no_finished_at_included(self):
        """Terminal task with no finished_at cannot be aged out — include it."""
        task = {"id": "t-9", "state": "done", "description": "Unknown age", "finished_at": None}
        result = _summarize_task(task, self._cutoff_ts())
        assert result is not None
        assert "description" not in result

    def test_terminal_task_invalid_finished_at_included(self):
        """Malformed finished_at should not cause exclusion."""
        task = {"id": "t-10", "state": "done", "description": "Bad ts", "finished_at": "not-a-date"}
        result = _summarize_task(task, self._cutoff_ts())
        assert result is not None
        assert "description" not in result

    def test_exactly_24h_plus_1s_boundary_excluded(self):
        """Tasks finished exactly 24h + 1s ago should be excluded."""
        task = {
            "id": "t-11",
            "state": "done",
            "description": "Boundary",
            "finished_at": self._iso(-(self._24H + 1)),  # 24h + 1s ago
        }
        result = _summarize_task(task, self._cutoff_ts())
        assert result is None

    def test_non_terminal_task_keeps_description(self):
        task = {"id": "t-12", "state": "working", "description": "Active work"}
        result = _summarize_task(task, self._cutoff_ts())
        assert result["description"] == "Active work"


# ---------------------------------------------------------------------------
# 7. truncate_tool_result — issue #96
# ---------------------------------------------------------------------------


class TestTruncateToolResult:
    def test_short_content_unchanged(self):
        content = "x" * 100
        assert truncate_tool_result(content) == content

    def test_content_at_limit_unchanged(self):
        content = "x" * MAX_TOOL_RESULT_CHARS
        assert truncate_tool_result(content) == content

    def test_content_over_limit_truncated(self):
        content = "x" * (MAX_TOOL_RESULT_CHARS + 500)
        result = truncate_tool_result(content)
        assert len(result) > MAX_TOOL_RESULT_CHARS  # includes the suffix
        assert result.startswith("x" * MAX_TOOL_RESULT_CHARS)
        assert "[truncated:" in result
        assert "500 chars omitted" in result

    def test_truncation_marker_format(self):
        content = "a" * (MAX_TOOL_RESULT_CHARS + 1)
        result = truncate_tool_result(content)
        assert result.endswith("\n… [truncated: 1 chars omitted]")

    def test_empty_string_unchanged(self):
        assert truncate_tool_result("") == ""

    def test_custom_max_chars(self):
        content = "y" * 200
        result = truncate_tool_result(content, max_chars=100)
        assert result == "y" * 100 + "\n… [truncated: 100 chars omitted]"

    def test_just_under_limit_unchanged(self):
        content = "z" * (MAX_TOOL_RESULT_CHARS - 1)
        assert truncate_tool_result(content) == content


# ---------------------------------------------------------------------------
# 8. prune_history — issue #98
# ---------------------------------------------------------------------------


class TestPruneHistory:
    def _make_messages(self, n: int) -> list[dict]:
        """Return n alternating user/assistant messages, starting with user."""
        msgs = []
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": f"message {i}"})
        return msgs

    def test_short_history_unchanged(self):
        msgs = self._make_messages(10)
        result = prune_history(msgs)
        assert result == msgs

    def test_exactly_max_unchanged(self):
        msgs = self._make_messages(MAX_HISTORY_MESSAGES)
        result = prune_history(msgs)
        assert result == msgs

    def test_over_limit_drops_oldest(self):
        msgs = self._make_messages(MAX_HISTORY_MESSAGES + 4)
        result = prune_history(msgs)
        assert len(result) <= MAX_HISTORY_MESSAGES

    def test_over_limit_keeps_newest(self):
        msgs = self._make_messages(MAX_HISTORY_MESSAGES + 2)
        result = prune_history(msgs)
        # Last message should be the last of the original list (or shifted by role fix)
        original_last = msgs[-1]["content"]
        assert any(m["content"] == original_last for m in result)

    def test_result_starts_with_user_turn(self):
        # Build list where pruning to last N would start with assistant
        msgs = []
        for i in range(MAX_HISTORY_MESSAGES + 3):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        # After pruning to MAX_HISTORY_MESSAGES, the first entry might be assistant
        result = prune_history(msgs)
        assert result[0]["role"] == "user"

    def test_empty_list_unchanged(self):
        assert prune_history([]) == []

    def test_single_user_message_unchanged(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert prune_history(msgs) == msgs

    def test_custom_max_messages(self):
        msgs = self._make_messages(10)
        result = prune_history(msgs, max_messages=4)
        assert len(result) <= 4
        assert result[0]["role"] == "user"

    def test_system_prompt_effectively_preserved(self):
        """Messages within the window are all kept; oldest beyond window are dropped.

        The system prompt is passed as a separate ``system=`` parameter in the
        Anthropic API call and is never part of the messages list, so it is
        automatically unaffected by pruning.
        """
        msgs = self._make_messages(MAX_HISTORY_MESSAGES + 5)
        result = prune_history(msgs)
        # First message in result must be user (API requirement)
        assert result[0]["role"] == "user"
        # We kept at most MAX_HISTORY_MESSAGES entries
        assert len(result) <= MAX_HISTORY_MESSAGES
