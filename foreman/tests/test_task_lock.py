"""Tests for the per-task asyncio.Lock in Foreman._run_task_trigger.

Mirrors backend/tests/test_foreman_guild_lock.py: verifies that concurrent
invocations for the same task_id are dropped (not queued) while invocations
for different tasks proceed independently, and that the lock is always
released — including when run_foreman_ai raises.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_foreman.config import Config
from pioneer_foreman.foreman import Foreman


def _make_foreman(**cfg) -> Foreman:
    config = Config(
        backend_url="ws://localhost:8000",
        guild_id="g1",
        backend_key="k",
        **cfg,
    )
    f = Foreman(config)
    f._http = AsyncMock()
    f._ws_send = AsyncMock()
    return f


async def test_concurrent_same_task_drops_second():
    f = _make_foreman()
    call_count = 0
    hold = asyncio.Event()

    async def _impl(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await hold.wait()

    with patch("pioneer_foreman.foreman.run_foreman_ai", side_effect=_impl):
        first = asyncio.create_task(f._run_task_trigger("g1", "msg1", "", None, "t-1"))
        await asyncio.sleep(0)  # let the first call acquire the lock

        await f._run_task_trigger("g1", "msg2", "", None, "t-1")

        hold.set()
        await first

    assert call_count == 1, f"Expected 1 run_foreman_ai call, got {call_count}"


async def test_concurrent_different_tasks_both_run():
    f = _make_foreman()
    call_log: list[str] = []
    hold = asyncio.Event()

    async def _impl(guild_id, human_message, *, task_id, **kwargs):
        call_log.append(task_id)
        await hold.wait()

    with patch("pioneer_foreman.foreman.run_foreman_ai", side_effect=_impl):
        t1 = asyncio.create_task(f._run_task_trigger("g1", "msg", "", None, "t-1"))
        t2 = asyncio.create_task(f._run_task_trigger("g1", "msg", "", None, "t-2"))
        await asyncio.sleep(0)
        hold.set()
        await asyncio.gather(t1, t2)

    assert sorted(call_log) == ["t-1", "t-2"]


async def test_lock_released_after_completion():
    f = _make_foreman()
    call_count = 0

    async def _impl(*args, **kwargs):
        nonlocal call_count
        call_count += 1

    with patch("pioneer_foreman.foreman.run_foreman_ai", side_effect=_impl):
        await f._run_task_trigger("g1", "first", "", None, "t-1")
        await f._run_task_trigger("g1", "second", "", None, "t-1")

    assert call_count == 2, "Both sequential calls should run"
    assert "t-1" not in f._task_locks, "Lock entry should be popped after release"


async def test_lock_released_after_run_foreman_ai_raises():
    f = _make_foreman()
    call_count = 0

    async def _impl(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")

    with patch("pioneer_foreman.foreman.run_foreman_ai", side_effect=_impl):
        # _run_task_trigger swallows the exception (it runs detached via
        # asyncio.create_task in production) but must still release the lock.
        await f._run_task_trigger("g1", "first", "", None, "t-1")
        await f._run_task_trigger("g1", "second", "", None, "t-1")

    assert call_count == 2
    assert "t-1" not in f._task_locks


async def test_run_task_trigger_passes_child_scoping():
    f = _make_foreman()
    calls = []

    async def _impl(guild_id, human_message, **kwargs):
        calls.append((guild_id, human_message, kwargs))

    with patch("pioneer_foreman.foreman.run_foreman_ai", side_effect=_impl):
        await f._run_task_trigger("g1", "hello", "ctx", "u-1", "t-1")

    assert len(calls) == 1
    guild_id, human_message, kwargs = calls[0]
    assert guild_id == "g1"
    assert human_message == "hello"
    assert kwargs["extra_context"] == "ctx"
    assert kwargs["user_id"] == "u-1"
    assert kwargs["task_id"] == "t-1"
    assert kwargs["child"] is True


# ── trigger dispatch (through the WS handler) ────────────────────────────


async def test_handle_trigger_routes_task_id_to_lock(monkeypatch):
    """A foreman-trigger carrying a taskId runs as a child-scoped, lock-serialised turn."""
    from tests.test_ws_integration import MockWebSocket

    trigger = {
        "type": "foreman-trigger",
        "guildId": "g1",
        "humanMessage": "task done",
        "taskId": "t-1",
        "event": "task-complete",
    }
    ws = MockWebSocket([trigger, {"type": "foreman-evicted", "reason": "done"}])

    calls = []

    async def fake_run(guild_id, human_message, **kwargs):
        calls.append(kwargs)
        return False

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", fake_run),
    ):
        f = _make_foreman()
        await f._run_connection()
        await asyncio.sleep(0.05)

    assert len(calls) == 1
    assert calls[0]["task_id"] == "t-1"
    assert calls[0]["child"] is True


async def test_handle_trigger_ignores_task_id_when_child_contexts_disabled(monkeypatch):
    from tests.test_ws_integration import MockWebSocket

    trigger = {
        "type": "foreman-trigger",
        "guildId": "g1",
        "humanMessage": "task done",
        "taskId": "t-1",
        "event": "task-complete",
    }
    ws = MockWebSocket([trigger, {"type": "foreman-evicted", "reason": "done"}])

    calls = []

    async def fake_run(guild_id, human_message, **kwargs):
        calls.append(kwargs)
        return False

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", fake_run),
    ):
        f = _make_foreman(child_contexts=False)
        await f._run_connection()
        await asyncio.sleep(0.05)

    assert len(calls) == 1
    # Falls through to the whole-guild parent path — no child scoping.
    assert calls[0].get("task_id") == "t-1"
    assert "child" not in calls[0]
