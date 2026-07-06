"""Tests for send_followup branch reuse (issue #680).

Invariant: when a worker executes a follow-up task it must always continue on
the task's existing branch — never generate a new one — so that pushes land on
the open PR instead of opening a second one.

There are two paths through which a follow-up task enters the worker queue:

  WS path:   backend broadcasts ``task-followup`` with a ``branch`` field.
             The _listen() handler stores it as ``followup_branch`` in the
             task dict, and ``_execute_task`` picks it up by that key.

  REST path: worker restarts and the idle puller fetches pending tasks from
             the REST endpoint.  The task dict is a ``row_to_dict(Task)``
             snapshot: it has the DB ``branch`` column and ``phase="followup"``
             but no ``followup_branch`` or ``followup_instructions`` (those
             existed only in the transient WS message).

             Before the fix, ``_execute_task`` looked only for
             ``followup_branch`` and fell through to generating a new name.
             The fix also reads ``task.get("branch")`` as a fallback and
             treats ``phase="followup"`` as the is_followup indicator.
"""

from __future__ import annotations

import os
import tempfile
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
        repos=["owner/repo"],
        **kwargs,
    )


# ── WS path ──────────────────────────────────────────────────────────────────


async def test_followup_ws_path_uses_existing_branch():
    """task-followup received via WS must attach a worktree on the existing
    branch, not create a new one.

    The task-followup WS handler stores the message's ``branch`` field as
    ``followup_branch`` in the queued task dict; _execute_task reads that key.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()
    slot = worker.agents[0]

    existing_branch = "claude/fix-something-t-abc123"
    task = {
        "id": "t-ws1",
        "name": "Fix something",
        "description": "original task description",
        "phase": "followup",
        "tool": "claude",
        "repos": ["owner/repo"],
        # WS path: the handler set followup_branch from msg["branch"]
        "followup_instructions": "address review comments",
        "followup_branch": existing_branch,
    }

    attached_branches: list[str] = []

    async def fake_attach(repo_path: str, wt_path: str, branch: str) -> bool:
        attached_branches.append(branch)
        return True

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch("pioneer_worker.worker.git_ops.attach_worktree", side_effect=fake_attach),
        patch("pioneer_worker.worker.github_pr.push_branch", return_value=True),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    assert attached_branches, "attach_worktree should have been called for a follow-up"
    assert attached_branches[0] == existing_branch, (
        f"Expected branch {existing_branch!r}, got {attached_branches[0]!r}; "
        "WS-path follow-up must continue on the existing branch"
    )


# ── REST / idle-puller path ───────────────────────────────────────────────────


async def test_followup_rest_path_uses_existing_branch():
    """Follow-up task picked up by the idle puller (REST) must attach a
    worktree on the existing branch stored in the DB ``branch`` column.

    The REST task dict has ``branch`` and ``phase="followup"`` but no
    ``followup_branch`` or ``followup_instructions``.  Before the fix,
    _execute_task ignored ``branch``, fell through the ``or`` to generate
    a new name, and pushed to a second PR.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()
    slot = worker.agents[0]

    existing_branch = "claude/fix-something-t-abc123"
    # Simulates row_to_dict(Task) output from the REST endpoint — snake_case
    # column names, no followup_branch / followup_instructions keys.
    task = {
        "id": "t-rest1",
        "worker_id": "w-test01",
        "guild_id": "test-guild",
        "name": "Fix something",
        "description": "original task description",
        "tool": "claude",
        "model": None,
        "provider": None,
        "phase": "followup",  # DB marker set by send_followup
        "issue_number": None,
        "issue_repo": None,
        "repos": [],
        "state": "working",
        "branch": existing_branch,  # DB column — the only branch reference
        "worktree_path": None,
        "pr_url": None,
        "pr_number": None,
        "pr_repo": None,
        # Note: NO followup_branch, NO followup_instructions
    }

    attached_branches: list[str] = []

    async def fake_attach(repo_path: str, wt_path: str, branch: str) -> bool:
        attached_branches.append(branch)
        return True

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch("pioneer_worker.worker.git_ops.attach_worktree", side_effect=fake_attach),
        patch("pioneer_worker.worker.github_pr.push_branch", return_value=True),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    assert attached_branches, (
        "attach_worktree should have been called for a follow-up task "
        "even when it arrives via the REST idle-puller path"
    )
    assert attached_branches[0] == existing_branch, (
        f"Expected branch {existing_branch!r}, got {attached_branches[0]!r}; "
        "idle-puller follow-up must continue on the existing branch (from DB), "
        "not generate a new one"
    )


async def test_new_task_still_generates_fresh_branch():
    """A new (non-follow-up) task must still derive a fresh branch name from
    its description and task_id — the fallback must not activate for new tasks.
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()
    slot = worker.agents[0]

    task = {
        "id": "t-new1",
        "name": "Add a new feature",
        "description": "Implement the new widget",
        "phase": "execute",
        "tool": "claude",
        "repos": ["owner/repo"],
        # No branch, no followup_branch, no followup_instructions
    }

    created_branches: list[str] = []

    async def fake_create(repo_path: str, wt_path: str, branch: str) -> bool:
        created_branches.append(branch)
        return True

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch("pioneer_worker.worker.git_ops.create_worktree", side_effect=fake_create),
        patch("pioneer_worker.worker.github_pr.push_branch", return_value=True),
        patch(
            "pioneer_worker.worker.github_pr.open_pr",
            return_value=("https://github.com/o/r/pull/1", 1),
        ),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    assert created_branches, "create_worktree should have been called for a new task"
    # Branch must start with "claude/" and contain the task_id prefix, not be empty
    assert created_branches[0].startswith("claude/"), (
        f"New task branch should start with 'claude/', got {created_branches[0]!r}"
    )
    assert "t-new1"[:6] in created_branches[0], (
        f"New task branch should contain the task_id prefix, got {created_branches[0]!r}"
    )


async def test_new_task_branch_contains_full_task_id_not_truncated():
    """Regression test for issue #775: branch names must embed the full
    6-character task_id suffix (e.g. ``t-b6u2we``), not a 4-character
    truncation (e.g. ``t-b6u2``).
    """
    worker = Worker(_make_cfg())
    worker._joined = True
    worker._send = AsyncMock()
    slot = worker.agents[0]

    task_id = "t-b6u2we"
    task = {
        "id": task_id,
        "name": "Fix branch name truncation",
        "description": "Ensure the full task id is used in branch names",
        "phase": "execute",
        "tool": "claude",
        "repos": ["owner/repo"],
        # No branch, no followup_branch, no followup_instructions
    }

    created_branches: list[str] = []

    async def fake_create(repo_path: str, wt_path: str, branch: str) -> bool:
        created_branches.append(branch)
        return True

    async def fake_run_claude(desc, *args, **kwargs):
        return True, "end_turn", "done", None

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", return_value="/tmp/fake-repo"),
        patch("pioneer_worker.worker.git_ops.create_worktree", side_effect=fake_create),
        patch("pioneer_worker.worker.github_pr.push_branch", return_value=True),
        patch(
            "pioneer_worker.worker.github_pr.open_pr",
            return_value=("https://github.com/o/r/pull/1", 1),
        ),
        patch("pioneer_worker.worker.github_pr.find_existing_pr", return_value=None),
        patch("pioneer_worker.worker.claude_runner.run_claude_auto", side_effect=fake_run_claude),
        tempfile.TemporaryDirectory() as tmp,
    ):
        worker.cfg.work_dir = tmp
        worker.cfg.repos_dir = tmp
        await worker._execute_task(task, slot)

    assert created_branches, "create_worktree should have been called for a new task"
    branch = created_branches[0]
    assert task_id in branch, (
        f"Branch name must contain the full task_id {task_id!r}, got {branch!r}; "
        "the branch suffix must not be truncated to 4 chars"
    )
    assert branch.endswith(f"-{task_id}"), (
        f"Branch name must end with the full '-{task_id}' suffix, got {branch!r}"
    )
