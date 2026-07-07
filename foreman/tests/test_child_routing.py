"""Unit tests for the parent Foreman's per-task child-context routing/spawn logic.

These poke the routing helpers directly (no WebSocket loop) and patch
run_foreman_ai out via the TaskContext layer.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pioneer_foreman.config import Config
from pioneer_foreman.foreman import Foreman


def _make_config(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="g1",
        backend_key="k",
        **kwargs,
    )


def _make_foreman(**cfg) -> Foreman:
    f = Foreman(_make_config(**cfg))
    f._http = AsyncMock()
    f._ws_send = AsyncMock()
    return f


def _norm(**kwargs) -> dict:
    base = {
        "guild_id": "g1",
        "human_message": "hi",
        "extra_context": "",
        "user_id": None,
        "task_id": None,
        "event": "?",
    }
    base.update(kwargs)
    return base


# ── normalize ──────────────────────────────────────────────────────────────


def test_normalize_trigger_maps_ws_fields():
    out = Foreman._normalize_trigger(
        {
            "guildId": "g1",
            "humanMessage": "do it",
            "extraContext": "ctx",
            "userId": "u-9",
            "taskId": "t-1",
            "event": "task-complete",
        }
    )
    assert out == {
        "guild_id": "g1",
        "human_message": "do it",
        "extra_context": "ctx",
        "user_id": "u-9",
        "task_id": "t-1",
        "event": "task-complete",
    }


# ── routing ────────────────────────────────────────────────────────────────


def test_route_no_task_id_goes_to_parent():
    f = _make_foreman()
    assert f._route_to_child(_norm(task_id=None)) is False


def test_route_unknown_task_goes_to_parent():
    f = _make_foreman()
    assert f._route_to_child(_norm(task_id="t-unknown")) is False


def test_route_disabled_goes_to_parent():
    f = _make_foreman(child_contexts=False)
    f._pending_for_task["t-1"] = []
    assert f._route_to_child(_norm(task_id="t-1")) is False


def test_route_to_live_child():
    f = _make_foreman()
    child = MagicMock()
    child.is_alive.return_value = True
    f._child_contexts["t-1"] = child
    trigger = _norm(task_id="t-1")
    assert f._route_to_child(trigger) is True
    child.enqueue.assert_called_once_with(trigger)


def test_route_buffers_during_pending_spawn():
    f = _make_foreman()
    f._pending_for_task["t-1"] = []
    trigger = _norm(task_id="t-1")
    assert f._route_to_child(trigger) is True
    assert f._pending_for_task["t-1"] == [trigger]


# ── spawn / observer ───────────────────────────────────────────────────────


async def test_observer_spawns_child_on_assign(monkeypatch):
    f = _make_foreman()
    # Keep TaskContext.run from doing anything real.
    monkeypatch.setattr("pioneer_foreman.task_context.run_foreman_ai", AsyncMock(return_value=True))
    f._parent_tool_observer(
        "assign_task",
        {"worker_id": "w-1"},
        {"content": "Task t-xyz assigned to w-1.", "is_error": False},
    )
    assert "t-xyz" in f._child_contexts
    assert f._child_contexts["t-xyz"].is_alive()
    await f._cancel_all_children()


async def test_observer_flushes_pending_on_spawn(monkeypatch):
    f = _make_foreman()
    monkeypatch.setattr("pioneer_foreman.task_context.run_foreman_ai", AsyncMock(return_value=True))
    # An event arrived for the task before assignment completed.
    early = _norm(task_id="t-xyz", human_message="early task-complete")
    f._pending_for_task["t-xyz"] = [early]
    f._parent_tool_observer(
        "assign_task",
        {"worker_id": "w-1"},
        {"content": "Task t-xyz queued for w-1.", "is_error": False},
    )
    child = f._child_contexts["t-xyz"]
    # The buffered trigger should have been flushed into the child's queue.
    assert child._queue.qsize() == 1
    assert "t-xyz" not in f._pending_for_task
    await f._cancel_all_children()


def test_observer_ignores_errored_assign():
    f = _make_foreman()
    f._parent_tool_observer(
        "assign_task",
        {"worker_id": "w-1"},
        {"content": "no idle worker", "is_error": True},
    )
    assert f._child_contexts == {}


def test_observer_ignores_non_assign_tools():
    f = _make_foreman()
    f._parent_tool_observer(
        "get_task_status",
        {"task_id": "t-1"},
        {"content": "Task t-1 working", "is_error": False},
    )
    assert f._child_contexts == {}


async def test_observer_tears_down_child_on_parent_finalize(monkeypatch):
    f = _make_foreman()
    child = MagicMock()
    child.stop = AsyncMock()
    f._child_contexts["t-1"] = child
    f._parent_tool_observer(
        "finalize_task",
        {"task_id": "t-1"},
        {"content": "Task t-1 finalized.", "is_error": False},
    )
    await asyncio.sleep(0)  # let the scheduled stop() task run
    child.stop.assert_awaited_once()


# ── teardown ───────────────────────────────────────────────────────────────


def test_deregister_child_clears_maps():
    f = _make_foreman()
    f._child_contexts["t-1"] = MagicMock()
    f._pending_for_task["t-1"] = []
    f._deregister_child("t-1")
    assert "t-1" not in f._child_contexts
    assert "t-1" not in f._pending_for_task
