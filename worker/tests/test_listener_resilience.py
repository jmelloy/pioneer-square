"""A single bad/unexpected WS message must not kill the listener task.

Before this, an exception anywhere in the big message-type dispatch (a KeyError
on a malformed backend message, a race like current_claude turning None between
a check and a use, ...) propagated out of _listen(), which run() treats as an
aux-task crash: it tears down every agent, closes the socket, and re-raises out
of Worker.run() — taking the whole process down over one bad message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_worker() -> Worker:
    worker = Worker(
        Config(
            backend_url="ws://localhost:8000",
            guild_id="test-guild",
            worker_id="w-test01",
            max_agents=1,
            pull_interval=3600.0,
        )
    )
    worker._send = AsyncMock()
    worker._joined = True
    return worker


@pytest.mark.asyncio
async def test_listener_keeps_processing_after_a_handler_exception():
    worker = _make_worker()

    calls = []

    async def _flaky_put(item):
        calls.append(item)
        if len(calls) == 1:
            raise RuntimeError("boom — simulated handler bug")

    worker.task_queue.put = _flaky_put

    async def _fake_messages():
        # First message's handling raises; the second must still be processed.
        yield {
            "type": "task-assigned",
            "workerId": "w-test01",
            "taskId": "t-1",
            "description": "first",
        }
        yield {
            "type": "task-assigned",
            "workerId": "w-test01",
            "taskId": "t-2",
            "description": "second",
        }

    worker.ws.messages = _fake_messages

    await worker._listen()

    assert [item["id"] for item in calls] == ["t-1", "t-2"]


@pytest.mark.asyncio
async def test_listener_survives_and_still_answers_a_later_ping():
    worker = _make_worker()

    async def _boom(_mtype, _msg):
        raise RuntimeError("boom")

    # Patch the first dispatch to explode; verify the *next* message (a
    # worker-ping) is still handled normally afterward.
    real_handle = worker._handle_ws_message
    call_count = 0

    async def _flaky_handle(mtype, msg):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom — simulated handler bug")
        return await real_handle(mtype, msg)

    worker._handle_ws_message = _flaky_handle

    async def _fake_messages():
        yield {"type": "worker-outdated", "workerId": "w-test01"}  # triggers the boom
        yield {"type": "worker-ping", "workerId": "w-test01"}

    worker.ws.messages = _fake_messages

    await worker._listen()

    sent_types = [m.args[0].get("type") for m in worker._send.await_args_list if m.args]
    assert "worker-pong" in sent_types
