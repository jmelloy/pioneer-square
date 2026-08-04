"""Tests for the pr_url auto-commit/auto-PR guard (issue #1093).

A task can carry a pr_url in its dict without being an explicit send_followup
continuation — e.g. the idle puller or a reconnect re-fetches it via the REST
pending-tasks endpoint (which dumps every DB column, including pr_url) while
its phase is still "execute". Without a guard, _execute_task treats that as a
brand-new task and auto-commits/pushes onto the existing PR branch. Only an
explicit follow-up (phase="followup" or followup_instructions set) should
push more commits onto an already-open PR.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, patch

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


async def _run_execute_task(task: dict):
    worker = Worker(_make_cfg(repos=["owner/repo"]))
    worker._joined = True
    worker._send = AsyncMock()
    worker.cfg.worker_id = "w-test01"
    slot = worker.agents[0]

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch("pioneer_worker.worker.git_ops.create_worktree", return_value=True),
        patch("pioneer_worker.worker.git_ops.attach_worktree", return_value=True),
        patch(
            "pioneer_worker.worker.github_pr.push_branch", return_value="pushed"
        ) as mock_push,
        patch(
            "pioneer_worker.worker.github_pr.find_existing_pr", return_value=None
        ) as mock_find,
        patch(
            "pioneer_worker.worker.github_pr.open_pr",
            return_value="https://github.com/owner/repo/pull/999",
        ) as mock_open_pr,
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    return worker, mock_push, mock_find, mock_open_pr


async def test_existing_pr_url_skips_auto_commit_when_not_followup():
    """A non-followup, non-review task that already has a pr_url must not
    auto-commit/push or open a new PR — it should reuse the existing pr_url."""
    task = {
        "id": "t-haspr",
        "name": "Implement feature",
        "description": "Add the new thing",
        "phase": "execute",
        "tool": "claude",
        "pr_url": "https://github.com/owner/repo/pull/42",
        "repos": ["owner/repo"],
    }

    worker, mock_push, mock_find, mock_open_pr = await _run_execute_task(task)

    mock_push.assert_not_called()
    mock_find.assert_not_called()
    mock_open_pr.assert_not_called()

    updates = [
        m.args[0]
        for m in worker._send.await_args_list
        if m.args and m.args[0].get("taskId") == "t-haspr"
    ]
    pr_urls = [u.get("prUrl") for u in updates if "prUrl" in u]
    assert "https://github.com/owner/repo/pull/42" in pr_urls, (
        "task-update must report the existing pr_url unchanged"
    )


async def test_followup_with_existing_pr_url_still_pushes():
    """An explicit send_followup continuation must still push additional
    commits onto the existing PR branch — the guard must not break it."""
    task = {
        "id": "t-followup",
        "name": "Implement feature",
        "description": "Fix CI failure",
        "phase": "followup",
        "tool": "claude",
        "pr_url": "https://github.com/owner/repo/pull/42",
        "followup_instructions": "fix the failing test",
        "followup_branch": "ps/feature-t-followup",
        "repos": ["owner/repo"],
    }

    worker, mock_push, mock_find, mock_open_pr = await _run_execute_task(task)

    mock_push.assert_awaited_once()


async def test_no_pr_url_still_pushes_normally():
    """A fresh task with no pr_url yet must go through the normal
    auto-commit/push/open-PR flow, unaffected by the new guard."""
    task = {
        "id": "t-fresh",
        "name": "Implement feature",
        "description": "Add the new thing",
        "phase": "execute",
        "tool": "claude",
        "repos": ["owner/repo"],
    }

    worker, mock_push, mock_find, mock_open_pr = await _run_execute_task(task)

    mock_push.assert_awaited_once()
    mock_open_pr.assert_awaited_once()
