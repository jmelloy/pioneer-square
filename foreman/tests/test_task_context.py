"""Unit tests for pioneer_foreman.task_context.TaskContext.

run_foreman_ai is patched out so these exercise the queue/lifecycle/observer
wiring without an LLM or backend.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_foreman.config import Config
from pioneer_foreman.task_context import TaskContext


def _make_config(**kwargs) -> Config:
    return Config(backend_url="ws://localhost:8000", guild_id="g1", backend_key="k", **kwargs)


def _make_ctx(on_finalize=None, **kwargs) -> TaskContext:
    return TaskContext(
        "t-abc",
        guild_id="g1",
        user_id="u-1",
        http=AsyncMock(),
        ws_send=AsyncMock(),
        config=_make_config(),
        on_finalize=on_finalize,
        **kwargs,
    )


async def _drain(ctx: TaskContext) -> None:
    """Wait until the context's queue is empty and the in-flight item is done."""
    await ctx._queue.join()


async def test_enqueue_runs_child_foreman():
    calls = []

    async def fake_run(guild_id, human_message, **kw):
        calls.append((guild_id, human_message, kw))
        return True

    ctx = _make_ctx()
    with patch("pioneer_foreman.task_context.run_foreman_ai", side_effect=fake_run):
        ctx.start()
        ctx.enqueue({"human_message": "task-complete for t-abc", "task_id": "t-abc"})
        await _drain(ctx)
        await ctx.stop()

    assert len(calls) == 1
    guild_id, msg, kw = calls[0]
    assert guild_id == "g1"
    assert msg == "task-complete for t-abc"
    # Child mode wiring
    assert kw["child"] is True
    assert kw["task_id"] == "t-abc"
    assert kw["tool_observer"] == ctx._observe_tool


async def test_empty_human_message_is_skipped():
    ran = []

    async def fake_run(*a, **k):
        ran.append(1)
        return True

    ctx = _make_ctx()
    with patch("pioneer_foreman.task_context.run_foreman_ai", side_effect=fake_run):
        ctx.start()
        ctx.enqueue({"task_id": "t-abc"})  # no human_message
        await _drain(ctx)
        await ctx.stop()

    assert ran == []


async def test_finalize_observer_triggers_teardown():
    finalized = []
    ctx = _make_ctx(on_finalize=finalized.append)

    async def fake_run(guild_id, human_message, *, tool_observer=None, **kw):
        # Simulate the child calling finalize_task successfully.
        tool_observer(
            "finalize_task",
            {"task_id": "t-abc"},
            {"content": "Task t-abc finalized.", "is_error": False},
        )
        return True

    with patch("pioneer_foreman.task_context.run_foreman_ai", side_effect=fake_run):
        ctx.start()
        ctx.enqueue({"human_message": "wrap it up", "task_id": "t-abc"})
        # Loop should exit on its own after the finalized turn.
        await asyncio.wait_for(ctx._task, timeout=2)

    assert ctx.is_alive() is False
    assert finalized == ["t-abc"]


async def test_observer_ignores_other_task_finalize():
    ctx = _make_ctx()
    ctx._observe_tool(
        "finalize_task",
        {"task_id": "t-other"},
        {"content": "Task t-other finalized.", "is_error": False},
    )
    assert ctx._finalized is False


async def test_observer_ignores_errored_finalize():
    ctx = _make_ctx()
    ctx._observe_tool(
        "finalize_task",
        {"task_id": "t-abc"},
        {"content": "boom", "is_error": True},
    )
    assert ctx._finalized is False


async def test_enqueue_returns_false_when_full():
    ctx = _make_ctx()
    # Don't start the loop, so nothing drains the queue.
    from pioneer_foreman.task_context import _CHILD_QUEUE_MAX

    for _ in range(_CHILD_QUEUE_MAX):
        assert ctx.enqueue({"human_message": "x", "task_id": "t-abc"}) is True
    assert ctx.enqueue({"human_message": "overflow", "task_id": "t-abc"}) is False


async def test_on_finalize_called_on_cancel():
    finalized = []
    ctx = _make_ctx(on_finalize=finalized.append)
    ctx.start()
    await asyncio.sleep(0)  # let the loop start and block on the queue
    await ctx.stop()
    assert finalized == ["t-abc"]
    assert ctx.is_alive() is False
