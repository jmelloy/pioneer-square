"""Tests for review-phase guard: phase field propagation and description injection.

Covers the bug where the task-assigned WS handler omitted the ``phase`` field
from the queued task dict, making ``(task.get("phase") or "").lower() == "review"``
always false in ``_execute_task``.
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


async def _pump_one_message(worker: Worker, msg: dict) -> None:
    async def one_shot():
        yield msg

    worker.ws.messages = one_shot
    await worker._listen()


# ── phase propagation from task-assigned ─────────────────────────────────────


async def test_task_assigned_captures_review_phase():
    """task-assigned with phase=review must land in the queue with phase='review'."""
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    msg = {
        "type": "task-assigned",
        "workerId": "w-test01",
        "taskId": "t-rev01",
        "name": "Review PR #42",
        "description": "Review the changes in PR #42",
        "tool": "claude",
        "phase": "review",
        "issueNumber": 42,
        "issueRepo": "owner/repo",
        "repos": [],
    }

    await _pump_one_message(worker, msg)

    assert worker.task_queue.qsize() == 1
    queued = worker.task_queue.get_nowait()
    assert queued["phase"] == "review", (
        "phase field must be captured from task-assigned message; "
        "without it the review guard in _execute_task is always false"
    )


async def test_task_assigned_defaults_phase_to_execute():
    """task-assigned without phase must default to 'execute', not None."""
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    msg = {
        "type": "task-assigned",
        "workerId": "w-test01",
        "taskId": "t-exec01",
        "name": "Implement feature",
        "description": "Add the new thing",
        "tool": "claude",
        "repos": [],
    }

    await _pump_one_message(worker, msg)

    assert worker.task_queue.qsize() == 1
    queued = worker.task_queue.get_nowait()
    assert queued["phase"] == "execute"


async def test_task_followup_captures_phase():
    """task-followup must forward the phase field so review semantics survive follow-ups."""
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    msg = {
        "type": "task-followup",
        "workerId": "w-test01",
        "taskId": "t-rev02",
        "name": "Review PR #42",
        "description": "Review the changes in PR #42",
        "instructions": "also check the migration",
        "branch": "claude/review-pr-t-rev02",
        "tool": "claude",
        "phase": "review",
        "repos": [],
    }

    await _pump_one_message(worker, msg)

    assert worker.task_queue.qsize() == 1
    queued = worker.task_queue.get_nowait()
    assert queued["phase"] == "review"


async def test_task_redirect_requeue_captures_phase():
    """task-redirect re-queued as a follow-up must carry the phase field."""
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()

    msg = {
        "type": "task-redirect",
        "workerId": "w-test01",
        "taskId": "t-rev03",
        "instructions": "focus on security issues",
        "branch": "claude/review-pr-t-rev03",
        "description": "Review PR #99",
        "phase": "review",
    }

    await _pump_one_message(worker, msg)

    assert worker.task_queue.qsize() == 1
    queued = worker.task_queue.get_nowait()
    assert queued["phase"] == "review"


# ── description injection in _execute_task ────────────────────────────────────


async def test_review_phase_injects_no_pr_instructions(caplog: pytest.LogCaptureFixture):
    """When phase='review', _execute_task must build a description that forbids
    opening a new PR, not the usual 'commit/push/gh pr create' instructions.

    We verify this by inspecting the description passed to the runner, which is
    captured in the emit log before runner launch.
    """
    import os
    import tempfile
    from unittest.mock import patch

    worker = Worker(_make_cfg(repos=["owner/repo"]))
    worker._joined = True
    worker._send = AsyncMock()
    worker.cfg.worker_id = "w-test01"

    task = {
        "id": "t-revguard",
        "name": "Review PR #42",
        "description": "Review the changes in PR #42",
        "phase": "review",
        "tool": "claude",
        "issue_number": 42,
        "issue_repo": "owner/repo",
        "repos": ["owner/repo"],
    }
    slot = worker.agents[0]

    captured_desc: list[str] = []

    async def fake_run_claude(desc, *args, **kwargs):
        captured_desc.append(desc)
        return True, "end_turn", "done"

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch("pioneer_worker.worker.git_ops.create_worktree", return_value=True),
        patch("pioneer_worker.worker.github_pr.push_branch", return_value=True),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.github_pr.branch_has_new_commits", return_value=False),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        patch.dict(os.environ, {}),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    assert captured_desc, "runner was never called"
    desc_sent = captured_desc[0]
    assert "gh pr create" not in desc_sent, (
        "review-phase task must NOT include 'gh pr create' instructions"
    )
    assert "NEVER" in desc_sent or "never" in desc_sent.lower(), (
        "review-phase description must explicitly forbid opening a new PR"
    )
    assert "gh pr review" in desc_sent, (
        "review-phase description must show how to post review comments with gh pr review"
    )
