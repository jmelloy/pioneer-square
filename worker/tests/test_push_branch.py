"""push_branch skips empty branches and reports real failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker import github_pr
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


async def _noop_emit(*_a, **_k) -> None:
    pass


@pytest.mark.asyncio
async def test_skips_push_when_no_new_commits():
    responses = {
        ("status", "--porcelain"): (0, "", ""),  # clean tree
        ("rev-parse", "--abbrev-ref"): (128, "", "no upstream"),  # first push
        ("rev-list", "--count"): (0, "0\n", ""),  # zero commits ahead
    }
    called = []

    async def _run_git(args, cwd=None):  # noqa: ANN001
        called.append(tuple(args[:2]))
        return responses.get(tuple(args[:2]), (0, "", ""))

    with patch.object(github_pr.git_ops, "run_git", new=_run_git):
        result = await github_pr.push_branch(
            branch="feat/x", worktree_path="/wt", token=None, emit=_noop_emit
        )

    assert result == "nothing"
    assert ("push", "-u") not in called  # never attempted the push


@pytest.mark.asyncio
async def test_pushes_when_commits_ahead():
    responses = {
        ("status", "--porcelain"): (0, "", ""),
        ("rev-parse", "--abbrev-ref"): (128, "", "no upstream"),
        ("rev-list", "--count"): (0, "2\n", ""),  # two commits ahead
        ("push", "-u"): (0, "", ""),
    }
    called = []

    async def _run_git(args, cwd=None):  # noqa: ANN001
        called.append(tuple(args[:2]))
        return responses.get(tuple(args[:2]), (0, "", ""))

    with patch.object(github_pr.git_ops, "run_git", new=_run_git):
        result = await github_pr.push_branch(
            branch="feat/x", worktree_path="/wt", token=None, emit=_noop_emit
        )

    assert result == "pushed"
    assert ("push", "-u") in called


@pytest.mark.asyncio
async def test_returns_failed_on_push_failure():
    responses = {
        ("status", "--porcelain"): (0, "", ""),
        ("rev-parse", "--abbrev-ref"): (128, "", "no upstream"),
        ("rev-list", "--count"): (0, "1\n", ""),
        ("push", "-u"): (1, "", "permission denied"),
    }

    async def _run_git(args, cwd=None):  # noqa: ANN001
        return responses.get(tuple(args[:2]), (0, "", ""))

    with patch.object(github_pr.git_ops, "run_git", new=_run_git):
        result = await github_pr.push_branch(
            branch="feat/x", worktree_path="/wt", token=None, emit=_noop_emit
        )

    assert result == "failed"


@pytest.mark.asyncio
async def test_success_but_no_commits_marks_error(tmp_path):
    """A 'successful' run that produced no commits (e.g. pi blocked on every
    tool call) must be flagged, not marked awaiting-review."""
    cfg = Config(
        backend_url="ws://localhost:8000",
        guild_id="g",
        worker_id="w",
        max_agents=1,
        pull_interval=3600.0,
        work_dir=str(tmp_path),
        repos=["org/myrepo"],
    )
    worker = Worker(cfg)
    worker._available_tools = ["pi"]
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    task = {"id": "t1", "description": "do it", "name": "do it", "tool": "pi"}
    agent = worker.agents[0]

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo",
            new=AsyncMock(return_value=str(tmp_path / "repo")),
        ),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.pi_runner.run_pi_auto",
            new=AsyncMock(return_value=(True, "success", "ok", None)),  # agent "succeeded"
        ),
        patch(
            "pioneer_worker.worker.github_pr.push_branch",
            new=AsyncMock(return_value="nothing"),  # ...but produced no commits
        ),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", new=AsyncMock(return_value=None)),
    ):
        await worker._execute_task(task, agent)

    states = [c.kwargs.get("state") for c in worker._task_update.await_args_list]
    assert "error" in states
    assert "awaiting-review" not in states
