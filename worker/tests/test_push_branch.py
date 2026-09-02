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
async def test_detached_followup_compares_and_pushes_head_to_branch():
    seen = []

    async def _run_git(args, cwd=None):  # noqa: ANN001
        seen.append(args)
        if args[:3] == ["rev-parse", "--verify", "origin/feat/x"]:
            return 0, "", ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "1\n", ""
        return 128 if args[:2] == ["rev-parse", "--abbrev-ref"] else 0, "", ""

    with patch.object(github_pr.git_ops, "run_git", new=_run_git):
        result = await github_pr.push_branch(
            branch="feat/x", worktree_path="/wt", token=None, emit=_noop_emit
        )

    assert result == "pushed"
    assert ["rev-list", "--count", "origin/feat/x..HEAD"] in seen
    assert ["push", "-u", "origin", "HEAD:feat/x"] in seen


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
async def test_success_with_dirty_tree_marks_error(tmp_path):
    """A 'successful' run that leaves the work tree dirty (e.g. pi blocked on
    every tool call, so push_branch's auto-commit had nothing committable to
    push) must be flagged, not marked awaiting-review. Only a dirty tree is a
    failure (#1259) — "nothing to push" with a clean tree is not, since the
    agent may have already committed and pushed earlier in the task."""
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
        patch(
            "pioneer_worker.worker.git_ops.run_git",
            new=AsyncMock(return_value=(0, "M some_file.py\n", "")),  # dirty tree
        ),
    ):
        await worker._execute_task(task, agent)

    states = [c.kwargs.get("state") for c in worker._task_update.await_args_list]
    assert "error" in states
    assert "awaiting-review" not in states
