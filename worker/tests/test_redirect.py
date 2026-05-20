"""Tests for task-redirect message handling.

The follow-up window inside ``_execute_task`` is gone — workers return to the
idle pool right after pushing a branch. The redirect handler now has two
clean cases:

  Active subprocess: SIGTERM the runner and queue the new instructions in
    the in-flight redirect_q so the redirect loop picks them up before
    returning the slot to idle.

  No active subprocess: the task is parked in awaiting-review (or fully
    closed) and the worker is idle. Re-queue the task into the worker's
    task_queue with a ``followup_instructions`` field so it gets picked up
    like a fresh assignment.
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


def _redirect_msg(
    task_id: str,
    instructions: str,
    worker_id: str = "w-test01",
    branch: str = "claude/test-branch",
) -> dict:
    return {
        "type": "task-redirect",
        "workerId": worker_id,
        "taskId": task_id,
        "instructions": instructions,
        "branch": branch,
        "description": "original task",
    }


async def _pump_one_message(worker: Worker, msg: dict) -> None:
    """Drive _listen() with a single message then stop."""

    async def one_shot():
        yield msg

    worker.ws.messages = one_shot
    await worker._listen()


async def test_redirect_buffered_in_redirect_queue_when_subprocess_just_exited():
    """Subprocess done, redirect_q still alive (the redirect loop hasn't
    drained yet). The redirect must land in redirect_q so the loop sees it.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-window1"
    slot = worker.agents[0]
    slot.current_task_id = task_id
    slot.current_claude = None

    redirect_q: asyncio.Queue = asyncio.Queue()
    worker._redirect_queues[task_id] = redirect_q

    await _pump_one_message(worker, _redirect_msg(task_id, "please add tests"))

    assert not redirect_q.empty(), "redirect must be buffered for in-flight redirect loop"
    assert redirect_q.get_nowait() == "please add tests"


async def test_redirect_re_queues_when_task_no_longer_running(caplog: pytest.LogCaptureFixture):
    """Task isn't on this worker anymore (no redirect_q). The handler must
    treat the redirect like a follow-up: enqueue a fresh task entry into
    task_queue so the agent loop picks it up. Nothing should be dropped.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-window2"

    with caplog.at_level(logging.INFO, logger="pioneer_worker.worker"):
        await _pump_one_message(worker, _redirect_msg(task_id, "one more thing"))

    assert worker.task_queue.qsize() == 1
    queued = worker.task_queue.get_nowait()
    assert queued["id"] == task_id
    assert queued["followup_instructions"] == "one more thing"
    assert queued["followup_branch"] == "claude/test-branch"


async def test_redirect_ignored_for_different_worker():
    """Redirects targeted at another worker must not affect our queues."""
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-other"
    redirect_q: asyncio.Queue = asyncio.Queue()
    worker._redirect_queues[task_id] = redirect_q

    await _pump_one_message(worker, _redirect_msg(task_id, "do something", worker_id="w-other99"))

    assert redirect_q.empty(), "redirect for another worker must not touch our queues"
    assert worker.task_queue.empty(), "redirect for another worker must not enqueue a task"


async def test_followup_message_enqueues_task_for_idle_worker():
    """A task-followup arriving at an idle worker should land in the task
    queue with branch + instructions so _execute_task can attach a worktree
    to the existing branch and resume work.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-followup1"
    msg = {
        "type": "task-followup",
        "workerId": "w-test01",
        "taskId": task_id,
        "name": "Add docs",
        "description": "original task description",
        "branch": "claude/add-docs-abc123",
        "instructions": "also add screenshots",
        "tool": "claude",
    }

    await _pump_one_message(worker, msg)

    assert worker.task_queue.qsize() == 1
    queued = worker.task_queue.get_nowait()
    assert queued["id"] == task_id
    assert queued["followup_instructions"] == "also add screenshots"
    assert queued["followup_branch"] == "claude/add-docs-abc123"
    assert queued["description"] == "original task description"


async def test_finalize_message_releases_worktrees_for_task():
    """task-finalize is now a hint that the worktree can be released early —
    it must NOT block any in-memory loop, just trigger cleanup of the
    registered worktrees for that task.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    task_id = "t-finalize1"

    released: list[str] = []

    async def fake_release(tid: str) -> None:
        released.append(tid)

    worker._release_task_worktrees = fake_release  # type: ignore[assignment]

    msg = {"type": "task-finalize", "workerId": "w-test01", "taskId": task_id}
    await _pump_one_message(worker, msg)

    assert released == [task_id]
