"""The worker's post-run downgrades of a runner's SUCCESS outcome (#1238).

A runner reporting success only means the agent finished its turn. Whether
that turn produced something reviewable is the worker's call, made after the
push: a failed push or an empty branch must not reach the backend as a
success. Both branches were previously untested — every _execute_task test
mocked push_branch with a bool, which is none of its three real return values
("pushed" | "nothing" | "failed") and fell silently through both branches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker.config import Config
from pioneer_worker.runner_types import StopReason  # pyright: ignore[reportMissingImports]
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


async def _run_with_push(tmp_path, push_result: str, *, dirty: bool = False) -> dict:
    """Run one successful claude task whose push returns *push_result*.

    Returns the kwargs of the final _task_update call — the outcome the
    backend would actually see.
    """
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    worker._release_task_worktrees = AsyncMock()  # type: ignore[assignment]

    task = {"id": "t-outcome", "description": "do something", "name": "do something"}
    dirty_status = (0, "M some_file.py\n" if dirty else "", "")
    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(return_value=(True, "success", "all done", "sess-1")),
        ),
        patch(
            "pioneer_worker.worker.github_pr.push_branch",
            new=AsyncMock(return_value=push_result),
        ),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", new=AsyncMock(return_value=None)),
        patch(
            "pioneer_worker.worker.git_ops.run_git",
            new=AsyncMock(return_value=dirty_status),
        ),
    ):
        await worker._execute_task(task, worker.agents[0])

    return worker._task_update.call_args.kwargs


@pytest.mark.asyncio
async def test_push_failure_downgrades_agent_success(tmp_path):
    """A genuine push failure must not surface as a reviewable result."""
    kwargs = await _run_with_push(tmp_path, "failed")

    assert kwargs["state"] == "error"
    assert kwargs["stopReason"] is StopReason.PUSH_FAILED
    assert "success" not in kwargs


@pytest.mark.asyncio
async def test_dirty_tree_after_no_push_downgrades_agent_success(tmp_path):
    """ "Success" with nothing pushed AND a dirty work tree is a real failure."""
    kwargs = await _run_with_push(tmp_path, "nothing", dirty=True)

    assert kwargs["state"] == "error"
    assert kwargs["stopReason"] is StopReason.NO_CHANGES


@pytest.mark.asyncio
async def test_nothing_to_push_with_clean_tree_keeps_agent_success(tmp_path):
    """Nothing new to push (e.g. already committed & pushed earlier) with a
    clean work tree is not a failure (#1259) — only a dirty tree is."""
    kwargs = await _run_with_push(tmp_path, "nothing", dirty=False)

    assert kwargs["success"] is True
    assert kwargs["stopReason"] is StopReason.SUCCESS


@pytest.mark.asyncio
async def test_pushed_branch_keeps_agent_success(tmp_path):
    """A real push leaves the runner's SUCCESS outcome intact."""
    kwargs = await _run_with_push(tmp_path, "pushed")

    assert kwargs["success"] is True
    assert kwargs["stopReason"] is StopReason.SUCCESS
    assert kwargs["lastText"] == "all done"
