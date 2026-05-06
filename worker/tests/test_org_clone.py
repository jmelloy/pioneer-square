"""Tests for org-based lazy clone logic.

Covers:
  - Task for an uncloned org repo triggers ensure_repo (clone)
  - Task for an already-cloned repo skips ensure_repo
  - Worker with repos list only still works (no org set)
  - _known_repos() includes lazily-cloned repos from the org dir
"""

from __future__ import annotations

import os
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


# ---------------------------------------------------------------------------
# Helper: build a minimal task dict
# ---------------------------------------------------------------------------


def _task(task_id: str, issue_repo: str | None = None, tool: str = "claude") -> dict:
    return {
        "id": task_id,
        "name": "test task",
        "description": "do something",
        "tool": tool,
        "issue_repo": issue_repo,
    }


# ---------------------------------------------------------------------------
# Lazy clone: org repo is cloned when task arrives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_repo_cloned_on_first_task(tmp_path):
    """When a task targets an org repo not yet on disk, ensure_repo is called."""
    worker = Worker(
        _make_cfg(
            org="myorg",
            repos=[],
            repos_dir=str(tmp_path / "repos"),
            work_dir=str(tmp_path / "work"),
        )
    )
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    slot = worker.slots[0]
    task = _task("t-org001", issue_repo="myorg/newrepo")

    cloned: list[str] = []

    async def fake_ensure_repo(repos_dir, repo_full, token):
        cloned.append(repo_full)
        repo_path = os.path.join(repos_dir, *repo_full.split("/", 1))
        os.makedirs(os.path.join(repo_path, ".git"), exist_ok=True)
        return repo_path

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", side_effect=fake_ensure_repo),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(return_value=(True, "end_turn", "done")),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.github_pr.ensure_pr",
            new=AsyncMock(return_value="https://github.com/myorg/newrepo/pull/1"),
        ),
        patch("pioneer_worker.worker.git_ops.ensure_webhook", new=AsyncMock()),
    ):
        await worker._execute_task(task, slot)

    assert "myorg/newrepo" in cloned, "ensure_repo must be called for the org repo"


# ---------------------------------------------------------------------------
# Idempotency: already-cloned repo skips the clone step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_cloned_repo_skips_clone(tmp_path):
    """ensure_repo is still called (it handles fetch internally), but the
    worktree is reused when the wt_path already exists."""
    repos_dir = tmp_path / "repos"
    work_dir = tmp_path / "work"

    worker = Worker(
        _make_cfg(
            org="myorg",
            repos=[],
            repos_dir=str(repos_dir),
            work_dir=str(work_dir),
        )
    )
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    slot = worker.slots[0]
    task_id = "t-org002"
    task = _task(task_id, issue_repo="myorg/existingrepo")

    # Pre-create the repo mirror and the worktree path to simulate an
    # already-cloned repo with an existing worktree.
    repo_path = repos_dir / "myorg" / "existingrepo"
    (repo_path / ".git").mkdir(parents=True)

    wt_path = work_dir / "test-guild" / "w-test01" / task_id / "existingrepo"
    (wt_path / ".git").mkdir(parents=True)

    ensure_repo_calls: list[str] = []

    async def fake_ensure_repo(repos_dir_, repo_full, token):
        ensure_repo_calls.append(repo_full)
        parts = repo_full.split("/", 1)
        return str(repos_dir / parts[0] / parts[1])

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", side_effect=fake_ensure_repo),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch("pioneer_worker.worker.git_ops.run_git", new=AsyncMock(return_value=(0, "", ""))),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(return_value=(True, "end_turn", "done")),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.github_pr.ensure_pr",
            new=AsyncMock(return_value="https://github.com/myorg/existingrepo/pull/1"),
        ),
        patch("pioneer_worker.worker.git_ops.ensure_webhook", new=AsyncMock()),
    ):
        await worker._execute_task(task, slot)

    # ensure_repo was called (it updates the mirror), but no new clone needed
    assert "myorg/existingrepo" in ensure_repo_calls


# ---------------------------------------------------------------------------
# Backwards compat: repos-list-only worker still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repos_only_worker_no_org(tmp_path):
    """A worker with repos=["owner/repo"] and no org works as before."""
    worker = Worker(
        _make_cfg(
            org=None,
            repos=["owner/myrepo"],
            repos_dir=str(tmp_path / "repos"),
            work_dir=str(tmp_path / "work"),
        )
    )
    worker._send = AsyncMock()
    worker._task_update = AsyncMock()

    slot = worker.slots[0]
    task = _task("t-norg01")  # no issue_repo

    cloned: list[str] = []

    async def fake_ensure_repo(repos_dir, repo_full, token):
        cloned.append(repo_full)
        repo_path = os.path.join(repos_dir, *repo_full.split("/", 1))
        os.makedirs(os.path.join(repo_path, ".git"), exist_ok=True)
        return repo_path

    with (
        patch("pioneer_worker.worker.git_ops.ensure_repo", side_effect=fake_ensure_repo),
        patch("pioneer_worker.worker.git_ops.create_worktree", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.claude_runner.run_claude_auto",
            new=AsyncMock(return_value=(True, "end_turn", "done")),
        ),
        patch("pioneer_worker.worker.github_pr.push_branch", new=AsyncMock(return_value=True)),
        patch(
            "pioneer_worker.worker.github_pr.ensure_pr",
            new=AsyncMock(return_value="https://github.com/owner/myrepo/pull/1"),
        ),
        patch("pioneer_worker.worker.git_ops.ensure_webhook", new=AsyncMock()),
    ):
        await worker._execute_task(task, slot)

    assert "owner/myrepo" in cloned
    assert len(cloned) == 1


# ---------------------------------------------------------------------------
# _known_repos: includes lazily-cloned repos from org dir
# ---------------------------------------------------------------------------


def test_known_repos_includes_org_repos_on_disk(tmp_path):
    """_known_repos() discovers repos already cloned under <repos_dir>/<org>/."""
    repos_dir = tmp_path / "repos"
    org_dir = repos_dir / "myorg"
    (org_dir / "repo1" / ".git").mkdir(parents=True)
    (org_dir / "repo2" / ".git").mkdir(parents=True)
    # A non-repo directory (no .git) should be excluded
    (org_dir / "notarepo").mkdir(parents=True)

    worker = Worker(
        _make_cfg(
            org="myorg",
            repos=[],
            repos_dir=str(repos_dir),
        )
    )
    known = worker._known_repos()
    assert "myorg/repo1" in known
    assert "myorg/repo2" in known
    assert "myorg/notarepo" not in known


def test_known_repos_with_static_repos_no_org(tmp_path):
    """Without org, _known_repos() returns only the static repos list."""
    worker = Worker(
        _make_cfg(
            org=None,
            repos=["owner/repo"],
            repos_dir=str(tmp_path / "repos"),
        )
    )
    known = worker._known_repos()
    assert known == ["owner/repo"]


def test_known_repos_combines_static_and_org(tmp_path):
    """With both repos and org, _known_repos() returns the union."""
    repos_dir = tmp_path / "repos"
    (repos_dir / "myorg" / "lazyrepo" / ".git").mkdir(parents=True)

    worker = Worker(
        _make_cfg(
            org="myorg",
            repos=["myorg/staticrepo"],
            repos_dir=str(repos_dir),
        )
    )
    known = worker._known_repos()
    assert "myorg/staticrepo" in known
    assert "myorg/lazyrepo" in known
    # No duplicates
    assert known.count("myorg/staticrepo") == 1


def test_known_repos_deduplicates_org_repos(tmp_path):
    """A repo already in the static list is not duplicated even if on disk."""
    repos_dir = tmp_path / "repos"
    (repos_dir / "myorg" / "myrepo" / ".git").mkdir(parents=True)

    worker = Worker(
        _make_cfg(
            org="myorg",
            repos=["myorg/myrepo"],
            repos_dir=str(repos_dir),
        )
    )
    known = worker._known_repos()
    assert known.count("myorg/myrepo") == 1
