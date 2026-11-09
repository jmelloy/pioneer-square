"""Mise installation is scoped to acquired task repositories and fails closed."""

import asyncio
import signal
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_worker import repo_setup
from pioneer_worker.config import Config
from pioneer_worker.task_worktree import AcquireResult, WorktreeEntry
from pioneer_worker.worker import Worker


async def test_no_config_skips_install(tmp_path):
    with patch.object(repo_setup.asyncio, "create_subprocess_exec") as spawn:
        assert await repo_setup.install_mise(str(tmp_path), AsyncMock())
    spawn.assert_not_called()


@pytest.mark.parametrize("config", repo_setup.MISE_CONFIGS)
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_install_detected_config(tmp_path, config, exit_code):
    path = tmp_path / config
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[tools]\n")
    proc = AsyncMock()
    proc.returncode = exit_code
    proc.communicate.return_value = (b"mise output\n", None)
    emit = AsyncMock()
    with patch.object(repo_setup.asyncio, "create_subprocess_exec", return_value=proc) as spawn:
        assert await repo_setup.install_mise(str(tmp_path), emit) == (exit_code == 0)
    spawn.assert_awaited_once_with(
        "mise",
        "install",
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    emit.assert_any_await("mise output", level="worker")


async def test_missing_mise_fails_with_message(tmp_path):
    (tmp_path / "mise.toml").touch()
    emit = AsyncMock()
    with patch.object(
        repo_setup.asyncio, "create_subprocess_exec", side_effect=FileNotFoundError("mise")
    ):
        assert not await repo_setup.install_mise(str(tmp_path), emit)
    assert "mise" in emit.await_args.args[0]


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_timeout_and_cancellation_reap_installer(tmp_path, error):
    (tmp_path / "mise.toml").touch()
    proc = AsyncMock()
    proc.returncode = None
    proc.pid = 12345
    proc.communicate.side_effect = error
    emit = AsyncMock()
    with (
        patch.object(repo_setup.asyncio, "create_subprocess_exec", return_value=proc),
        patch.object(repo_setup.os, "killpg") as kill,
    ):
        if error is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await repo_setup.install_mise(str(tmp_path), emit)
        else:
            assert not await repo_setup.install_mise(str(tmp_path), emit)
            assert "timed out" in emit.await_args.args[0]
    kill.assert_called_once_with(proc.pid, signal.SIGKILL)
    proc.wait.assert_awaited_once()


@pytest.mark.parametrize("setup_ok", [True, False])
async def test_worker_prepares_only_acquired_repos_before_agent(tmp_path, setup_ok):
    worker = Worker(
        Config(
            backend_url="ws://localhost:8000",
            guild_id="test",
            worker_id="w-test",
            max_agents=1,
            work_dir=str(tmp_path),
            repos=["org/unused"],
        )
    )
    worker._available_tools = ["pi"]
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    entries = [
        WorktreeEntry(f"org/{name}", f"/cache/{name}", str(tmp_path / name))
        for name in ("one", "two")
    ]
    prepared = []

    async def install(worktree, emit):
        prepared.append(worktree)
        return setup_ok

    async def run(*args, **kwargs):
        assert prepared == [entry.wt_path for entry in entries]
        return True, "success", "ok", None

    with (
        patch(
            "pioneer_worker.worker.git_ops.ensure_repo", new=AsyncMock(return_value="/cache/repo")
        ),
        patch.object(
            worker.worktrees,
            "acquire",
            new=AsyncMock(
                return_value=AcquireResult(
                    entries,
                    entries[0].wt_path,
                    [],
                )
            ),
        ),
        patch("pioneer_worker.worker.publish.check_access", new=AsyncMock()),
        patch("pioneer_worker.worker.repo_setup.install_mise", side_effect=install),
        patch("pioneer_worker.worker.pi_runner.run_pi_auto", side_effect=run) as runner,
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value="pushed")),
    ):
        await worker._execute_task(
            {
                "id": "t-setup",
                "name": "setup",
                "description": "test",
                "repos": ["org/one", "org/two"],
            },
            worker.agents[0],
        )
    if setup_ok:
        runner.assert_awaited_once()
    else:
        runner.assert_not_called()
        assert prepared == [entries[0].wt_path]
        assert any(
            call.kwargs.get("state") == "failed" for call in worker._task_update.await_args_list
        )
        assert worker.agents[0].state == "idle"
