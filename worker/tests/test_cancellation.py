"""Cancelled tasks retain worktrees for recovery until the TTL sweeper runs."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import _CANCEL_SENTINEL, Worker


@pytest.fixture(autouse=True)
def app_publication_available():
    with patch("pioneer_worker.worker.publish.check_access", new=AsyncMock()):
        yield


def _make_cfg(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test-guild",
        worker_id="w-test01",
        max_agents=1,
        pull_interval=3600.0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_cancelled_task_retains_worktrees(tmp_path):
    """Cancellation must not discard uncommitted work."""
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task_id = "t-cancel-wt1"
    agent = worker.agents[0]

    worker.worktrees.release = AsyncMock()  # type: ignore[method-assign]

    # Mark the task cancelled so the post-run check fires immediately.
    worker._mark_cancelled(task_id)

    task = {"id": task_id, "description": "do something", "name": "do something"}

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(return_value=(False, "interrupted", "", None)),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")),
    ):
        await worker._execute_task(task, agent)

    worker.worktrees.release.assert_not_called()
    assert task_id in worker.worktrees
    assert agent.state == "idle", "slot must return to idle after cancellation"


@pytest.mark.asyncio
async def test_cancel_sentinel_retains_worktrees(tmp_path):
    """A cancel sentinel must leave the task's worktrees registered."""
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task_id = "t-cancel-wt2"
    slot = worker.agents[0]

    worker.worktrees.release = AsyncMock()  # type: ignore[method-assign]

    task = {"id": task_id, "description": "do something", "name": "do something"}

    async def mock_run(*args, **kwargs):
        # By the time the runner is called, _redirect_queues[task_id] is live.
        rq = worker._redirect_queues.get(task_id)
        if rq is not None:
            await rq.put(_CANCEL_SENTINEL)
        return (False, "interrupted", "", None)

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", new=mock_run),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")),
    ):
        await worker._execute_task(task, slot)

    worker.worktrees.release.assert_not_called()
    assert task_id in worker.worktrees
    assert slot.state == "idle", "slot must return to idle after cancel sentinel"


@pytest.mark.asyncio
async def test_cancellation_is_cleared_once_honoured(tmp_path):
    """A cancelled id must not stay cancelled forever (#1238).

    _cancelled_tasks used to be add-only: after one cancellation the worker
    refused that task id for the rest of its lifetime, even on a fresh foreman
    re-assignment, and the set grew without bound.
    """
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task_id = "t-cancel-clear"
    task = {"id": task_id, "description": "do something", "name": "do something"}
    worker._mark_cancelled(task_id)
    assert worker._assignment_capacity_reason(task) == f"task {task_id} is cancelled"

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(return_value=(False, "interrupted", "", None)),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")),
    ):
        await worker._execute_task(task, worker.agents[0])

    assert task_id not in worker._cancelled_tasks
    assert worker._assignment_capacity_reason(task) is None, "a re-assignment must be accepted"


def test_mark_cancelled_evicts_oldest_past_the_cap():
    """The residual set is bounded, so a long-lived worker can't leak it."""
    from pioneer_worker.worker import _MAX_CANCELLED_TASKS

    worker = Worker(_make_cfg())
    for i in range(_MAX_CANCELLED_TASKS + 10):
        worker._mark_cancelled(f"t-{i}")

    assert len(worker._cancelled_tasks) == _MAX_CANCELLED_TASKS
    assert "t-0" not in worker._cancelled_tasks, "oldest entries must be evicted"
    assert f"t-{_MAX_CANCELLED_TASKS + 9}" in worker._cancelled_tasks
