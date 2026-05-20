"""Worker emits the slot's identifying IDs on every agent-state message.

These tests guard against the regression where ``agent-state`` only carried
``agentId`` + ``state``. The frontend now matches task rows to agents by
``agent.taskId``, so each agent-state must include:

- ``workerId``: which worker process the slot belongs to
- ``agentId``: the slot's id (== ``Agent.id``)
- ``taskId``: the slot's current task (or ``None`` when idle/offline)
- ``state`` / ``activity``: the runtime state

It also verifies that ``_set_state`` clears ``slot.current_task_id`` on
transitions to ``idle`` and ``offline``, so the frontend can't latch onto
a stale task id from a previous run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_worker() -> Worker:
    cfg = Config(
        backend_url="ws://localhost:8000",
        guild_id="g-test",
        worker_id="w-test01",
        max_agents=2,
        pull_interval=3600.0,
    )
    worker = Worker(cfg)
    worker._send = AsyncMock()
    return worker


def _last_sent(worker: Worker) -> dict:
    """Return the payload of the most recent ``_send`` call."""
    assert worker._send.await_count > 0, "no _send calls were made"
    args, _ = worker._send.await_args
    return args[0]


@pytest.mark.asyncio
async def test_set_state_working_emits_all_three_ids():
    """`working` state must broadcast workerId, agentId, taskId together."""
    worker = _make_worker()
    slot = worker.agents[0]
    slot.current_task_id = "t-abc123"

    await worker._set_state("working", slot)

    msg = _last_sent(worker)
    assert msg["type"] == "agent-state"
    assert msg["workerId"] == "w-test01"
    assert msg["agentId"] == slot.agent_id
    assert msg["taskId"] == "t-abc123"
    assert msg["state"] == "working"


@pytest.mark.asyncio
async def test_set_state_idle_clears_task_id_on_slot_and_message():
    """Transitioning to idle must null the slot's task link and the message."""
    worker = _make_worker()
    slot = worker.agents[0]
    slot.current_task_id = "t-abc123"
    slot.activity = "editing"

    await worker._set_state("idle", slot)

    assert slot.current_task_id is None, "idle slot must drop its task link"
    assert slot.activity is None, "idle slot must drop its activity"
    msg = _last_sent(worker)
    assert msg["state"] == "idle"
    assert msg["taskId"] is None
    assert msg["activity"] is None
    # workerId/agentId stay populated for routing.
    assert msg["workerId"] == "w-test01"
    assert msg["agentId"] == slot.agent_id


@pytest.mark.asyncio
async def test_set_state_offline_clears_task_id_on_slot_and_message():
    """Offline transitions (shutdown) must also drop the stale task link."""
    worker = _make_worker()
    slot = worker.agents[0]
    slot.current_task_id = "t-zzz"

    await worker._set_state("offline", slot)

    assert slot.current_task_id is None
    msg = _last_sent(worker)
    assert msg["state"] == "offline"
    assert msg["taskId"] is None


@pytest.mark.asyncio
async def test_set_state_error_preserves_task_id():
    """Error state is still tied to the task that failed; don't drop it."""
    worker = _make_worker()
    slot = worker.agents[0]
    slot.current_task_id = "t-failing"

    await worker._set_state("error", slot)

    assert slot.current_task_id == "t-failing"
    msg = _last_sent(worker)
    assert msg["state"] == "error"
    assert msg["taskId"] == "t-failing"


@pytest.mark.asyncio
async def test_task_emit_activity_change_includes_ids():
    """The activity-change branch of ``_task_emit`` must mirror the same IDs.

    Regression: previously this path emitted only agentId/state/activity,
    so the frontend would receive an activity flip without enough context
    to keep the agent-to-task mapping in sync after a reconnect.
    """
    worker = _make_worker()
    slot = worker.agents[0]
    slot.current_task_id = "t-emit1"
    slot.state = "working"
    slot.activity = "reading"

    emit = worker._task_emit("t-emit1", slot)
    await emit("hello", detail={"activity": "editing"})

    # _task_emit calls _send twice: once for terminal-output, once for
    # the activity-change agent-state. The agent-state message is last.
    msg = _last_sent(worker)
    assert msg["type"] == "agent-state"
    assert msg["workerId"] == "w-test01"
    assert msg["agentId"] == slot.agent_id
    assert msg["taskId"] == "t-emit1"
    assert msg["state"] == "working"
    assert msg["activity"] == "editing"
    assert slot.activity == "editing"


@pytest.mark.asyncio
async def test_task_emit_no_activity_change_does_not_send_state():
    """Same activity → no spurious agent-state emit (only terminal-output)."""
    worker = _make_worker()
    slot = worker.agents[0]
    slot.current_task_id = "t-emit2"
    slot.state = "working"
    slot.activity = "editing"

    emit = worker._task_emit("t-emit2", slot)
    await emit("noop", detail={"activity": "editing"})

    # Only the terminal-output frame; no agent-state delta.
    sent_types = [c.args[0]["type"] for c in worker._send.await_args_list]
    assert sent_types == ["terminal-output"]


@pytest.mark.asyncio
async def test_concurrent_slots_emit_distinct_ids():
    """Two slots on the same worker must emit distinct agentIds with the
    same workerId — the regression that caused multiple agents to render
    as 'working' on one bench depended on slots being indistinguishable
    on the wire."""
    worker = _make_worker()
    slot_a, slot_b = worker.agents
    slot_a.current_task_id = "t-A"
    slot_b.current_task_id = "t-B"

    await worker._set_state("working", slot_a)
    msg_a = _last_sent(worker)
    await worker._set_state("working", slot_b)
    msg_b = _last_sent(worker)

    assert msg_a["workerId"] == msg_b["workerId"] == "w-test01"
    assert msg_a["agentId"] != msg_b["agentId"]
    assert msg_a["taskId"] == "t-A"
    assert msg_b["taskId"] == "t-B"
