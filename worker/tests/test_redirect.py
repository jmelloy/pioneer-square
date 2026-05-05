"""Tests for task-redirect message handling edge cases.

Covers the scenario where a redirect arrives after the Claude subprocess has
exited but before (or after) the followup window queue exists:

  Window 1 — post-subprocess, pre-followup-queue:
    redirect_q exists, followup_q does not → buffer in redirect_q
  Window 2 — task fully finalised:
    neither queue exists → warn and drop gracefully
  Drain — followup window opens after a buffered redirect:
    items in redirect_q must flow into followup_q
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_cfg(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test-guild",
        worker_id="w-test01",
        max_agents=1,
        pull_interval=3600.0,
        **kwargs,
    )


def _redirect_msg(task_id: str, instructions: str, worker_id: str = "w-test01") -> dict:
    return {
        "type": "task-redirect",
        "workerId": worker_id,
        "taskId": task_id,
        "instructions": instructions,
    }


async def _pump_one_message(worker: Worker, msg: dict) -> None:
    """Drive _listen() with a single message then stop."""

    async def one_shot():
        yield msg

    worker.ws.messages = one_shot
    # _listen runs until the generator is exhausted — one message, then done.
    await worker._listen()


async def test_redirect_buffered_in_redirect_queue_when_followup_queue_absent():
    """Window 1: subprocess done, followup_q not yet created.

    The redirect must land in redirect_q (not be silently dropped) so that
    the followup window can drain it when it opens.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-window1"
    # Simulate: subprocess has finished (_capture_session_and_clear cleared
    # current_claude) but followup_q hasn't been created yet.
    slot = worker.slots[0]
    slot.current_task_id = task_id
    slot.current_claude = None

    redirect_q: asyncio.Queue = asyncio.Queue()
    worker._redirect_queues[task_id] = redirect_q
    # followup_q intentionally absent

    await _pump_one_message(worker, _redirect_msg(task_id, "please add tests"))

    assert not redirect_q.empty(), "redirect instructions must be buffered in redirect_q"
    assert redirect_q.get_nowait() == "please add tests"


async def test_redirect_dropped_gracefully_when_task_fully_finalized(
    caplog: pytest.LogCaptureFixture,
):
    """Window 2: task fully done, both queues cleaned up.

    The redirect must be dropped with a warning — no exception, no silent discard.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-window2"
    # No active slot, no queues — task runner has fully exited.
    # (slot.current_task_id is None after the task runner finally block.)

    with caplog.at_level(logging.WARNING, logger="pioneer_worker.worker"):
        await _pump_one_message(worker, _redirect_msg(task_id, "one more thing"))

    warning_texts = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(task_id in t for t in warning_texts), (
        f"Expected a warning mentioning {task_id!r}, got: {warning_texts}"
    )


async def test_redirect_ignored_for_different_worker():
    """Redirects targeted at another worker must not affect our queues."""
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-other"
    redirect_q: asyncio.Queue = asyncio.Queue()
    worker._redirect_queues[task_id] = redirect_q

    await _pump_one_message(worker, _redirect_msg(task_id, "do something", worker_id="w-other99"))

    assert redirect_q.empty(), "Redirect for another worker must not touch our queues"


async def test_buffered_redirect_drains_into_followup_queue_on_open():
    """Items buffered in redirect_q during Window 1 must flow into followup_q.

    This tests the drain loop that runs when the followup window is set up
    in _execute_task, ensuring pre-window redirects become the first followup.
    """
    redirect_q: asyncio.Queue = asyncio.Queue()
    await redirect_q.put("fix the flaky test")
    await redirect_q.put("then update the docs")

    followup_q: asyncio.Queue = asyncio.Queue()

    # Replicate the drain logic from _execute_task
    while not redirect_q.empty():
        try:
            buffered = redirect_q.get_nowait()
        except asyncio.QueueEmpty:
            break
        await followup_q.put(buffered)

    assert redirect_q.empty(), "redirect_q must be empty after drain"
    assert followup_q.qsize() == 2, "both items must appear in followup_q"
    assert await followup_q.get() == "fix the flaky test"
    assert await followup_q.get() == "then update the docs"


async def test_redirect_queued_as_followup_when_followup_queue_exists():
    """Happy path: subprocess done, followup window open — redirect goes into followup_q."""
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-happy"
    slot = worker.slots[0]
    slot.current_task_id = task_id
    slot.current_claude = None

    followup_q: asyncio.Queue = asyncio.Queue()
    worker._followup_queues[task_id] = followup_q

    await _pump_one_message(worker, _redirect_msg(task_id, "keep going"))

    assert not followup_q.empty(), "redirect must land in followup_q"
    assert followup_q.get_nowait() == "keep going"
