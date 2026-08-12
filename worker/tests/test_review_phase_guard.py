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
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch("pioneer_worker.worker.git_ops.get_pr_head_branch", return_value="pr-42-branch"),
        patch("pioneer_worker.worker.git_ops.checkout_pr_worktree", return_value=True),
        patch("pioneer_worker.worker.github_pr.push_branch", return_value=True),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
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


async def test_review_phase_checks_out_pr_branch_via_gh(caplog: pytest.LogCaptureFixture):
    """Review-phase tasks must check out the PR's own branch (via `gh pr checkout`,
    wrapped by ``git_ops.checkout_pr_worktree``) instead of generating a new
    ``ps/...`` branch, and must never push commits (issue #799).
    """
    import os
    import tempfile
    from unittest.mock import patch

    worker = Worker(_make_cfg(repos=["owner/repo"]))
    worker._joined = True
    worker._send = AsyncMock()
    worker.cfg.worker_id = "w-test01"

    task = {
        "id": "t-revcheckout",
        "name": "Review PR #42",
        "description": "Review the changes in PR #42",
        "phase": "review",
        "tool": "claude",
        "issue_number": 42,
        "issue_repo": "owner/repo",
        "repos": ["owner/repo"],
    }
    slot = worker.agents[0]

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch(
            "pioneer_worker.worker.git_ops.get_pr_head_branch", return_value="feature/pr-42-branch"
        ) as mock_get_branch,
        patch(
            "pioneer_worker.worker.git_ops.checkout_pr_worktree", return_value=True
        ) as mock_checkout,
        patch("pioneer_worker.worker.git_ops.create_worktree") as mock_create_worktree,
        patch("pioneer_worker.worker.github_pr.push_branch") as mock_push,
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        patch.dict(os.environ, {}),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    expected_wt_path = os.path.join(tmp, "test-guild", "w-test01", "t-revcheckout", "repo")
    mock_get_branch.assert_awaited_once_with("owner/repo", 42)
    mock_checkout.assert_awaited_once_with(
        "/tmp/fake-repo", expected_wt_path, 42, "owner/repo", None
    )
    mock_create_worktree.assert_not_called()
    mock_push.assert_not_called()

    updates = [
        m
        for m in worker._send.await_args_list
        if m.args and m.args[0].get("branch") == "feature/pr-42-branch"
    ]
    assert updates, "task record must be updated with the PR's actual branch, not a generated one"
    assert not any(
        m.args[0].get("branch", "").startswith("ps/") for m in worker._send.await_args_list
    ), "review tasks must never record a generated ps/... branch"


async def test_review_prefers_pr_number_over_issue_number():
    """When pr_number/pr_repo are present they identify the PR to check out —
    issue_number is the GitHub issue to close, a different number (issue #843)."""
    import os
    import tempfile
    from unittest.mock import patch

    worker = Worker(_make_cfg(repos=["owner/repo"]))
    worker._joined = True
    worker._send = AsyncMock()
    worker.cfg.worker_id = "w-test01"

    task = {
        "id": "t-revpr",
        "name": "Review PR #99",
        "description": "Review PR #99",
        "phase": "review",
        "tool": "claude",
        "issue_number": 42,  # the GitHub issue — must NOT be used as the PR number
        "issue_repo": "owner/other",
        "pr_number": 99,
        "pr_repo": "owner/repo",
        "repos": ["owner/repo"],
    }
    slot = worker.agents[0]

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch(
            "pioneer_worker.worker.git_ops.get_pr_head_branch", return_value="feature/pr-99"
        ) as mock_get_branch,
        patch("pioneer_worker.worker.git_ops.checkout_pr_worktree", return_value=True) as mock_co,
        patch("pioneer_worker.worker.git_ops.create_worktree") as mock_create_worktree,
        patch("pioneer_worker.worker.github_pr.push_branch"),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    expected_wt_path = os.path.join(tmp, "test-guild", "w-test01", "t-revpr", "repo")
    mock_get_branch.assert_awaited_once_with("owner/repo", 99)
    mock_co.assert_awaited_once_with("/tmp/fake-repo", expected_wt_path, 99, "owner/repo", None)
    mock_create_worktree.assert_not_called()
    completion_msgs = [
        m.args[0]
        for m in worker._send.await_args_list
        if m.args and m.args[0].get("taskId") == "t-revpr"
    ]
    assert any(
        m.get("type") == "task-complete"
        and m.get("prUrl") == "https://github.com/owner/repo/pull/99"
        for m in completion_msgs
    ), "review tasks must report a PR URL from pr_repo/pr_number even if branch lookup finds none"


# ── metadata handoff preflight guard (issue #1124) ────────────────────────────


async def test_review_task_missing_metadata_aborts_before_checkout():
    """A review task with no pr_repo/pr_number and no issue_repo/issue_number
    fallback must fail fast, before ever touching git, instead of reaching
    git_ops.get_pr_head_branch with None arguments and later logging the
    confusing 'Could not resolve PR branch for review' error (issue #1124:
    tasks t-b7hmav, t-v1kftf, t-d5d4ru, t-g7d9ja reached the worker with
    issue_repo=null, branch=null, pr_url=null).
    """
    from unittest.mock import patch

    worker = Worker(_make_cfg(repos=["owner/repo"]))
    worker._joined = True
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()
    worker.cfg.worker_id = "w-test01"

    task = {
        "id": "t-revnometa",
        "name": "Review PR",
        "description": "Review the changes",
        "phase": "review",
        "tool": "claude",
        # No issue_repo/issue_number, no pr_repo/pr_number, no branch/pr_url.
        "repos": [],
    }
    slot = worker.agents[0]

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo") as mock_ensure_repo,
        patch("pioneer_worker.worker.git_ops.get_pr_head_branch") as mock_get_branch,
    ):
        await worker._execute_task(task, slot)

    mock_get_branch.assert_not_called()
    mock_ensure_repo.assert_not_called()

    failed_calls = [
        c for c in worker._task_update.await_args_list if c.kwargs.get("state") == "failed"
    ]
    assert failed_calls, "task must be marked failed when review metadata is missing"
    assert slot.state == "idle", "slot must return to idle after refusing the task"


async def test_review_task_1758_regression_resolves_branch_from_metadata():
    """Regression test for issue #1124 / PR #1758 (Identity-Digital/dnsid):
    when assign_task carries pr_repo/pr_number through the metadata handoff,
    the worker must use them to resolve the PR's head branch rather than
    aborting with null metadata.
    """
    import os
    import tempfile
    from unittest.mock import patch

    worker = Worker(_make_cfg(repos=["Identity-Digital/dnsid"]))
    worker._joined = True
    worker._send = AsyncMock()
    worker.cfg.worker_id = "w-test01"

    task = {
        "id": "t-g7d9ja",
        "name": "Review PR #1758",
        "description": "Review the changes in PR #1758",
        "phase": "review",
        "tool": "claude",
        "pr_repo": "Identity-Digital/dnsid",
        "pr_number": 1758,
        "pr_head_ref": "wolfgang/issue-1481-provider-budget",
        "pr_url": "https://github.com/Identity-Digital/dnsid/pull/1758",
        "head_sha": "7c10afdc25388b3a59588a79cad075f640ce6611",
        "repos": ["Identity-Digital/dnsid"],
    }
    slot = worker.agents[0]

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch(
            "pioneer_worker.worker.git_ops.get_pr_head_branch",
            return_value="wolfgang/issue-1481-provider-budget",
        ) as mock_get_branch,
        patch("pioneer_worker.worker.git_ops.checkout_pr_worktree", return_value=True),
        patch("pioneer_worker.worker.git_ops.create_worktree") as mock_create_worktree,
        patch("pioneer_worker.worker.github_pr.push_branch") as mock_push,
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    mock_get_branch.assert_awaited_once_with("Identity-Digital/dnsid", 1758)
    mock_create_worktree.assert_not_called()
    mock_push.assert_not_called()
