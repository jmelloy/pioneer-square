"""A mid-task message must reach the agent running the task it was meant for.

A worker runs up to ``max_agents`` tasks at once, so "the running agent" is
ambiguous. Delivering to an arbitrary one injects a task's instructions into an
unrelated task's session, which is worse than not delivering at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_worker(slots: int) -> Worker:
    worker = Worker(
        Config(
            backend_url="ws://localhost:8000",
            guild_id="test-guild",
            worker_id="w-test01",
            max_agents=slots,
            pull_interval=3600.0,
        )
    )
    worker._send = AsyncMock()
    return worker


def _running(worker: Worker, idx: int, task_id: str) -> AsyncMock:
    proc = AsyncMock()
    proc.send_message = AsyncMock(return_value=True)
    worker.agents[idx].current_claude = proc
    worker.agents[idx].current_task_id = task_id
    return proc


@pytest.mark.asyncio
async def test_message_goes_to_the_agent_running_the_named_task():
    worker = _make_worker(3)
    first = _running(worker, 0, "t-aaa")
    second = _running(worker, 1, "t-bbb")
    third = _running(worker, 2, "t-ccc")

    await worker._inject_worker_message("focus on the migration", "t-bbb")

    second.send_message.assert_awaited_once_with("focus on the migration")
    first.send_message.assert_not_awaited()
    third.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_for_a_task_running_elsewhere_is_dropped():
    worker = _make_worker(2)
    first = _running(worker, 0, "t-aaa")
    second = _running(worker, 1, "t-bbb")

    await worker._inject_worker_message("hello", "t-not-here")

    first.send_message.assert_not_awaited()
    second.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_untargeted_message_is_delivered_when_only_one_agent_runs():
    worker = _make_worker(4)
    only = _running(worker, 2, "t-aaa")

    await worker._inject_worker_message("carry on", None)

    only.send_message.assert_awaited_once_with("carry on")


@pytest.mark.asyncio
async def test_untargeted_message_is_dropped_when_several_agents_run():
    worker = _make_worker(2)
    first = _running(worker, 0, "t-aaa")
    second = _running(worker, 1, "t-bbb")

    await worker._inject_worker_message("ambiguous", None)

    first.send_message.assert_not_awaited()
    second.send_message.assert_not_awaited()
    # The operator is told why, and which tasks were candidates.
    lines = [
        m.args[0]["line"]
        for m in worker._send.await_args_list
        if m.args and m.args[0].get("type") == "terminal-output"
    ]
    assert any("not delivered" in line and "t-aaa" in line and "t-bbb" in line for line in lines)


@pytest.mark.asyncio
async def test_message_with_no_agent_running_is_a_no_op():
    worker = _make_worker(2)
    await worker._inject_worker_message("nobody home", None)
    await worker._inject_worker_message("nobody home", "t-aaa")
    assert worker._send.await_count == 0
