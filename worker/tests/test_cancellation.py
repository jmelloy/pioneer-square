"""Tests that cancelled tasks release their worktrees immediately.

On cancellation the worker must NOT defer worktree removal to the TTL sweeper;
it must call _release_task_worktrees right away so disk space is reclaimed and
a fresh assignment to the same worktree path is not confused by stale state.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import _CANCEL_SENTINEL, Worker


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
async def test_cancelled_task_releases_worktrees_immediately(tmp_path):
    """When _cancelled_tasks contains the task id after the runner exits,
    _release_task_worktrees must be called before the slot returns to idle.
    """
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task_id = "t-cancel-wt1"
    slot = worker.slots[0]

    released: list[str] = []

    async def tracking_release(tid: str) -> None:
        released.append(tid)

    worker._release_task_worktrees = tracking_release  # type: ignore[assignment]

    # Mark the task cancelled so the post-run check fires immediately.
    worker._cancelled_tasks.add(task_id)

    task = {"id": task_id, "description": "do something", "name": "do something"}

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(return_value=(False, "interrupted", "")),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value=False)),
    ):
        await worker._execute_task(task, slot)

    assert task_id in released, "worktrees must be released immediately on cancellation"
    assert slot.state == "idle", "slot must return to idle after cancellation"


@pytest.mark.asyncio
async def test_cancel_sentinel_releases_worktrees_immediately(tmp_path):
    """When _CANCEL_SENTINEL lands in the redirect queue after the runner
    exits, _release_task_worktrees must be called before the slot returns to idle.
    """
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task_id = "t-cancel-wt2"
    slot = worker.slots[0]

    released: list[str] = []

    async def tracking_release(tid: str) -> None:
        released.append(tid)

    worker._release_task_worktrees = tracking_release  # type: ignore[assignment]

    task = {"id": task_id, "description": "do something", "name": "do something"}

    async def mock_run(*args, **kwargs):
        # By the time the runner is called, _redirect_queues[task_id] is live.
        rq = worker._redirect_queues.get(task_id)
        if rq is not None:
            await rq.put(_CANCEL_SENTINEL)
        return (False, "interrupted", "")

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", new=mock_run),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value=False)),
    ):
        await worker._execute_task(task, slot)

    assert task_id in released, "worktrees must be released immediately on cancel sentinel"
    assert slot.state == "idle", "slot must return to idle after cancel sentinel"
