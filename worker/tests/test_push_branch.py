"""The worker fallback shares the agent's verified App checkpoint publisher."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker import github_pr, publish
from pioneer_worker.config import Config
from pioneer_worker.runner_types import RunResult, StopReason
from pioneer_worker.task_worktree import AcquireResult, WorktreeEntry
from pioneer_worker.worker import Worker


async def _noop_emit(*_a, **_k) -> None:
    pass


@pytest.mark.parametrize("result", ["pushed", "nothing"])
async def test_fallback_uses_shared_publisher(result):
    context = publish.PublishContext("https://backend", "g", "t", "secret", "feat/x")
    with patch.object(
        publish, "publish_checkpoint", new=AsyncMock(return_value=result)
    ) as checkpoint:
        assert (
            await github_pr.push_branch(worktree_path="/wt", context=context, emit=_noop_emit)
            == result
        )
    checkpoint.assert_awaited_once_with(
        "/wt", context, "chore: save task progress [auto-checkpoint]", fallback_token=None
    )


async def test_fallback_does_not_use_git_push_on_failure():
    context = publish.PublishContext("https://backend", "g", "t", "secret", "feat/x")
    with (
        patch.object(
            publish,
            "publish_checkpoint",
            new=AsyncMock(side_effect=publish.PublishError("invalid signature")),
        ),
        patch.object(github_pr.git_ops, "run_git", new=AsyncMock()) as run_git,
    ):
        assert (
            await github_pr.push_branch(worktree_path="/wt", context=context, emit=_noop_emit)
            == "failed"
        )
    run_git.assert_not_awaited()


@pytest.mark.parametrize("tool", ["claude", "pi", "codex"])
@pytest.mark.parametrize("phase", ["execute", "followup", "plan"])
async def test_every_runner_gets_publication_instructions_and_scoped_context(tmp_path, tool, phase):
    worker = Worker(
        Config(
            backend_url="ws://localhost:8000",
            guild_id="g",
            worker_id="w",
            auth_token="worker-secret",
            repos=["owner/repo"],
            work_dir=str(tmp_path),
        )
    )
    worker._available_tools = [tool]
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    worker._task_github_token = AsyncMock(return_value="human-token")
    worker._runners[tool] = SimpleNamespace(
        run=AsyncMock(return_value=RunResult(True, StopReason.SUCCESS))
    )
    worker.worktrees.acquire = AsyncMock(
        return_value=AcquireResult(
            [WorktreeEntry("owner/repo", str(tmp_path), str(tmp_path))], str(tmp_path), []
        )
    )
    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo", new=AsyncMock(return_value=str(tmp_path))
        ),
        patch("pioneer_worker.worker.publish.check_access", new=AsyncMock()) as access,
        patch(
            "pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")
        ) as fallback,
        patch("pioneer_worker.worker.github_pr.find_existing_pr", new=AsyncMock(return_value=None)),
    ):
        await worker._execute_task(
            {"id": "t-context", "description": "task", "tool": tool, "phase": phase},
            worker.agents[0],
        )
    request = worker._runners[tool].run.call_args.args[0]
    assert "pioneer publish -m" in request.description
    assert "git add -A && git commit" not in request.description
    assert request.env["PIONEER_TASK_ID"] == "t-context"
    assert request.env["PIONEER_AUTH_TOKEN"] == "worker-secret"
    assert request.env["GITHUB_TOKEN"] == "human-token"  # PR credentials stay separate.
    context = fallback.call_args.kwargs["context"]
    assert fallback.call_args.kwargs["token"] == "human-token"
    assert request.env["PIONEER_PUBLISH_BRANCH"] == context.branch
    access.assert_awaited_once_with(str(tmp_path), context)


async def test_secondary_repo_publication_does_not_open_empty_primary_pr(tmp_path):
    worker = Worker(
        Config(
            backend_url="ws://localhost:8000",
            guild_id="g",
            worker_id="w",
            auth_token="secret",
            repos=["owner/primary", "owner/secondary"],
            work_dir=str(tmp_path),
        )
    )
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    worker._task_github_token = AsyncMock(return_value=None)
    worker._runners["pi"] = SimpleNamespace(
        run=AsyncMock(return_value=RunResult(True, StopReason.SUCCESS))
    )
    entries = [
        WorktreeEntry(f"owner/{name}", str(tmp_path / name), str(tmp_path / name))
        for name in ("primary", "secondary")
    ]
    worker.worktrees.acquire = AsyncMock(
        return_value=AcquireResult(entries, entries[0].wt_path, [])
    )
    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo", new=AsyncMock(return_value=str(tmp_path))
        ),
        patch("pioneer_worker.worker.git_ops.run_git", new=AsyncMock(return_value=(0, "", ""))),
        patch("pioneer_worker.worker.publish.check_access", new=AsyncMock()),
        patch(
            "pioneer_worker.worker.github_pr.push_branch",
            new=AsyncMock(side_effect=["nothing", "pushed"]),
        ) as checkpoints,
        patch("pioneer_worker.worker.github_pr.find_existing_pr", new=AsyncMock(return_value=None)),
        patch("pioneer_worker.worker.github_pr.open_pr", new=AsyncMock()) as open_pr,
    ):
        await worker._execute_task(
            {"id": "t-multi-publish", "description": "task", "tool": "pi", "create_pr": True},
            worker.agents[0],
        )
    assert checkpoints.await_count == 2
    open_pr.assert_not_awaited()


@pytest.mark.parametrize("error_type", [publish.SigningUnavailable, publish.PublishError])
async def test_preflight_warns_on_unavailability_but_stops_on_other_errors(tmp_path, error_type):
    worker = Worker(
        Config(
            backend_url="ws://localhost:8000",
            guild_id="g",
            worker_id="w",
            repos=["owner/repo"],
            work_dir=str(tmp_path),
        )
    )
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    worker._task_github_token = AsyncMock(return_value=None)
    runner = SimpleNamespace(run=AsyncMock(return_value=RunResult(True, StopReason.SUCCESS)))
    worker._runners["pi"] = runner
    worker.worktrees.acquire = AsyncMock(
        return_value=AcquireResult(
            [WorktreeEntry("owner/repo", str(tmp_path), str(tmp_path))], str(tmp_path), []
        )
    )
    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo", new=AsyncMock(return_value=str(tmp_path))
        ),
        patch(
            "pioneer_worker.worker.publish.check_access",
            new=AsyncMock(side_effect=error_type("not installed")),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", new=AsyncMock(return_value=None)),
    ):
        await worker._execute_task(
            {"id": "t-no-app", "description": "task", "tool": "pi"}, worker.agents[0]
        )
    if error_type is publish.SigningUnavailable:
        runner.run.assert_awaited_once()
        assert worker._task_update.call_args.kwargs["state"] != "failed"
    else:
        runner.run.assert_not_awaited()
        assert worker._task_update.call_args.kwargs["state"] == "failed"


@pytest.mark.asyncio
async def test_success_with_dirty_tree_marks_error(tmp_path):
    """An agent success with an unpublished dirty worktree is not reviewable."""
    cfg = Config(
        backend_url="ws://localhost:8000",
        guild_id="g",
        worker_id="w",
        auth_token="worker-secret",
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
        patch("pioneer_worker.worker.publish.check_access", new=AsyncMock()),
        patch(
            "pioneer_worker.worker.pi_runner.run_pi_auto",
            new=AsyncMock(return_value=(True, "success", "ok", None)),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="nothing")),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", new=AsyncMock(return_value=None)),
        patch(
            "pioneer_worker.worker.git_ops.run_git",
            new=AsyncMock(return_value=(0, "M some_file.py\n", "")),
        ),
    ):
        await worker._execute_task(task, agent)
    states = [c.kwargs.get("state") for c in worker._task_update.await_args_list]
    assert "error" in states
    assert "awaiting-review" not in states
