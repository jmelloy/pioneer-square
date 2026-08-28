"""Tests for the worker's tool-dispatch guard (issue #827).

A worker only ever runs the tool binaries it actually detected/was
configured with (``self._available_tools``). ``_execute_task`` must never
fall back to "claude" just because a task omits ``tool``, and must refuse
outright rather than silently invoking a tool the worker doesn't have.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


@pytest.mark.asyncio
async def test_pi_only_worker_never_invokes_claude_when_task_tool_omitted(tmp_path):
    """A worker with only 'pi' available must dispatch to pi, not claude,
    even when the task dict has no 'tool' field at all.
    """
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._available_tools = ["pi"]
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task = {"id": "t-pi01", "description": "do something", "name": "do something"}
    agent = worker.agents[0]

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.pi_runner.run_pi_auto",
            new=AsyncMock(return_value=(True, "success", "ok", None)),
        ) as mock_pi,
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(side_effect=AssertionError("claude must not be invoked")),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")),
    ):
        await worker._execute_task(task, agent)

    mock_pi.assert_awaited_once()


@pytest.mark.asyncio
async def test_pi_only_worker_refuses_task_that_explicitly_requests_claude(tmp_path):
    """If a task explicitly asks for a tool the worker doesn't have, the
    worker must abort the task rather than silently running claude anyway.
    """
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._available_tools = ["pi"]
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task = {
        "id": "t-pi02",
        "description": "do something",
        "name": "do something",
        "tool": "claude",
    }
    agent = worker.agents[0]

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(side_effect=AssertionError("claude must not be invoked")),
        ),
        patch(
            "pioneer_worker.worker.pi_runner.run_pi_auto",
            new=AsyncMock(side_effect=AssertionError("pi must not be invoked either")),
        ),
    ):
        await worker._execute_task(task, agent)

    failed_calls = [
        c for c in worker._task_update.await_args_list if c.kwargs.get("state") == "failed"
    ]
    assert failed_calls, "task must be marked failed when the requested tool is unavailable"
    assert agent.state == "idle", "slot must return to idle after refusing the task"


@pytest.mark.asyncio
async def test_multi_tool_worker_dispatches_to_requested_pi_tool(tmp_path):
    """A worker with both claude and pi available must honor an explicit
    tool='pi' request rather than defaulting to claude.
    """
    worker = Worker(_make_cfg(work_dir=str(tmp_path), repos=["org/myrepo"]))
    worker._available_tools = ["claude", "pi"]
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    task = {"id": "t-multi01", "description": "do something", "name": "do something", "tool": "pi"}
    agent = worker.agents[0]

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.pi_runner.run_pi_auto",
            new=AsyncMock(return_value=(True, "success", "ok", None)),
        ) as mock_pi,
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(side_effect=AssertionError("claude must not be invoked")),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")),
    ):
        await worker._execute_task(task, agent)

    mock_pi.assert_awaited_once()
