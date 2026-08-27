"""Unit tests for TaskWorktree — the on-disk worktree set for one task.

These run against real git repos under ``tmp_path`` (local filesystem clones,
no network) with no ``Worker``, no WebSocket, and no mocking of git_ops: every
git operation TaskWorktree performs actually runs, so a bug in the reuse
heuristic or the attach/create fallback shows up as a real assertion failure
against real files on disk rather than a mismatched mock call.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from pioneer_worker.task_worktree import AcquireResult, TaskWorktree

pytestmark = pytest.mark.asyncio


def _git(args: list[str], cwd: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


def _init_origin(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=main"], cwd=str(path))
    _git(["config", "user.email", "test@example.com"], cwd=str(path))
    _git(["config", "user.name", "Test"], cwd=str(path))
    (path / "README.md").write_text("hello\n")
    _git(["add", "."], cwd=str(path))
    _git(["commit", "-m", "initial"], cwd=str(path))


def _clone(origin: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", str(origin), str(dest)], check=True, capture_output=True, text=True
    )


def _push_new_branch(origin: Path, branch: str, filename: str, content: str) -> None:
    """Simulate a prior worker having pushed a branch, via a throwaway clone."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch"
        _clone(origin, scratch)
        _git(["checkout", "-b", branch], cwd=str(scratch))
        (scratch / filename).write_text(content)
        _git(["add", "."], cwd=str(scratch))
        _git(["commit", "-m", f"add {filename}"], cwd=str(scratch))
        _git(["push", "origin", branch], cwd=str(scratch))


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


@pytest.fixture
def origin(tmp_path) -> Path:
    origin_path = tmp_path / "origin.git"
    _init_origin(origin_path)
    return origin_path


@pytest.fixture
def repo_path(tmp_path, origin) -> Path:
    dest = tmp_path / "cache" / "acme" / "widgets"
    _clone(origin, dest)
    return dest


def _tw(tmp_path, *, ttl_seconds: float = 3600.0, clock=None) -> TaskWorktree:
    base = tmp_path / "work"
    return TaskWorktree(
        base_dir=lambda: str(base), ttl_seconds=ttl_seconds, clock=clock or FakeClock()
    )


def _primary(result: AcquireResult) -> str:
    assert result.primary is not None
    return result.primary


# ── acquire: fresh ──────────────────────────────────────────────────────────


async def test_acquire_fresh_creates_worktree(tmp_path, repo_path):
    tw = _tw(tmp_path)
    result = await tw.acquire(
        "t-1", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/do-thing-t-1"
    )
    assert result.failed == []
    primary = _primary(result)
    assert os.path.isdir(os.path.join(primary, ".git")) or os.path.isfile(
        os.path.join(primary, ".git")
    )
    assert result.entries[0].repo_full == "acme/widgets"
    assert "t-1" in tw


async def test_acquire_reuses_existing_worktree_without_recreating(tmp_path, repo_path):
    tw = _tw(tmp_path)
    first = await tw.acquire(
        "t-2", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/x-t-2"
    )
    marker = Path(_primary(first)) / "marker.txt"
    marker.write_text("still here")

    second = await tw.acquire(
        "t-2", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/x-t-2"
    )
    assert second.primary == first.primary
    assert marker.read_text() == "still here"


async def test_acquire_unknown_mode_raises(tmp_path, repo_path):
    tw = _tw(tmp_path)
    with pytest.raises(ValueError):
        await tw.acquire("t-x", [("acme/widgets", str(repo_path))], mode="bogus", branch="b")


async def test_acquire_reports_failed_repo_without_aborting_others(tmp_path, repo_path):
    tw = _tw(tmp_path)
    # A repo_path that exists but isn't a git repo — `git fetch` fails cleanly
    # (rc != 0) rather than the worktree op ever getting attempted.
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = await tw.acquire(
        "t-3",
        [
            ("acme/widgets", str(repo_path)),
            ("acme/missing", str(not_a_repo)),
        ],
        mode="fresh",
        branch="ps/y-t-3",
    )
    assert result.failed == ["acme/missing"]
    assert result.primary is not None
    assert [e.repo_full for e in result.entries] == ["acme/widgets"]


# ── acquire: followup ───────────────────────────────────────────────────────


async def test_acquire_followup_attaches_existing_branch(tmp_path, origin, repo_path):
    _push_new_branch(origin, "feature/foo", "feature.txt", "v1")
    tw = _tw(tmp_path)

    result = await tw.acquire(
        "t-4", [("acme/widgets", str(repo_path))], mode="followup", branch="feature/foo"
    )
    assert result.failed == []
    assert (Path(_primary(result)) / "feature.txt").read_text() == "v1"


async def test_acquire_followup_attaches_when_branch_checked_out_elsewhere(
    tmp_path, origin, repo_path
):
    _push_new_branch(origin, "feature/shared", "feature.txt", "v1")
    tw = _tw(tmp_path)
    first = await tw.acquire(
        "t-a", [("acme/widgets", str(repo_path))], mode="followup", branch="feature/shared"
    )
    second = await tw.acquire(
        "t-b", [("acme/widgets", str(repo_path))], mode="followup", branch="feature/shared"
    )

    assert first.failed == []
    assert second.failed == []
    assert (Path(_primary(second)) / "feature.txt").read_text() == "v1"


async def test_acquire_followup_falls_back_to_create_when_branch_missing(tmp_path, repo_path):
    tw = _tw(tmp_path)
    result = await tw.acquire(
        "t-5",
        [("acme/widgets", str(repo_path))],
        mode="followup",
        branch="feature/never-pushed",
    )
    # attach_worktree fails (branch never reached origin) — create_worktree
    # fallback should still produce a usable worktree.
    assert result.failed == []
    assert result.primary is not None


async def test_acquire_followup_reuse_pulls_latest_from_origin(tmp_path, origin, repo_path):
    _push_new_branch(origin, "feature/bar", "feature.txt", "v1")
    tw = _tw(tmp_path)
    first = await tw.acquire(
        "t-6", [("acme/widgets", str(repo_path))], mode="followup", branch="feature/bar"
    )
    assert (Path(_primary(first)) / "feature.txt").read_text() == "v1"

    # A second worker (or the same one) pushes a follow-up commit.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch2"
        _clone(origin, scratch)
        _git(["checkout", "feature/bar"], cwd=str(scratch))
        (scratch / "feature.txt").write_text("v2")
        _git(["add", "."], cwd=str(scratch))
        _git(["commit", "-m", "v2"], cwd=str(scratch))
        _git(["push", "origin", "feature/bar"], cwd=str(scratch))

    second = await tw.acquire(
        "t-6", [("acme/widgets", str(repo_path))], mode="followup", branch="feature/bar"
    )
    assert second.primary == first.primary
    assert (Path(_primary(second)) / "feature.txt").read_text() == "v2"


# ── release ──────────────────────────────────────────────────────────────────


async def test_release_removes_worktree_and_forgets_task(tmp_path, repo_path):
    tw = _tw(tmp_path)
    result = await tw.acquire(
        "t-7", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/z-t-7"
    )
    primary = _primary(result)
    assert os.path.isdir(primary)

    await tw.release("t-7")

    assert "t-7" not in tw
    assert not os.path.isdir(primary)


async def test_release_of_unknown_task_is_a_noop(tmp_path):
    tw = _tw(tmp_path)
    await tw.release("nope")  # must not raise


# ── TTL / sweep ──────────────────────────────────────────────────────────────


async def test_touch_extends_ttl_so_sweep_leaves_it(tmp_path, repo_path):
    clock = FakeClock()
    tw = _tw(tmp_path, ttl_seconds=100.0, clock=clock)
    await tw.acquire("t-8", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/t8")

    clock.advance(90.0)
    tw.touch("t-8")
    clock.advance(90.0)  # 90s since touch — still under the 100s TTL

    released = await tw.sweep(active_task_ids=set())
    assert released == []
    assert "t-8" in tw


async def test_sweep_releases_worktrees_past_ttl(tmp_path, repo_path):
    clock = FakeClock()
    tw = _tw(tmp_path, ttl_seconds=100.0, clock=clock)
    result = await tw.acquire(
        "t-9", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/t9"
    )

    clock.advance(101.0)
    released = await tw.sweep(active_task_ids=set())

    assert released == ["t-9"]
    assert "t-9" not in tw
    assert not os.path.isdir(_primary(result))


async def test_sweep_skips_tasks_still_active(tmp_path, repo_path):
    clock = FakeClock()
    tw = _tw(tmp_path, ttl_seconds=100.0, clock=clock)
    await tw.acquire("t-10", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/t10")

    clock.advance(101.0)
    released = await tw.sweep(active_task_ids={"t-10"})

    assert released == []
    assert "t-10" in tw


# ── startup reclamation ────────────────────────────────────────────────────


async def test_reclaim_startup_re_registers_fresh_dirs(tmp_path, repo_path):
    clock = FakeClock()
    tw = _tw(tmp_path, ttl_seconds=1000.0, clock=clock)
    result = await tw.acquire(
        "t-11", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/t11"
    )
    task_dir = tw.task_dir("t-11")

    # reclaim_startup ages a task dir off its real filesystem mtime (there's
    # no other way to know how old someone else's leftover directory is), so
    # this is backdated against wall-clock time, not the injected FakeClock.
    age = 400.0
    mtime = time.time() - age
    os.utime(task_dir, (mtime, mtime))

    # Simulate a fresh process: a brand-new TaskWorktree with an empty registry.
    fresh_tw = _tw(tmp_path, ttl_seconds=1000.0, clock=clock)
    removed = await fresh_tw.reclaim_startup([("acme/widgets", str(repo_path))])

    assert removed == []
    assert "t-11" in fresh_tw
    assert os.path.isdir(_primary(result))

    # It should behave as if it had been active `age` seconds ago: sweeping
    # with less than (ttl - age) more elapsed leaves it, past it retires it.
    clock.advance(1000.0 - age - 10)
    assert await fresh_tw.sweep(active_task_ids=set()) == []
    clock.advance(20)
    assert await fresh_tw.sweep(active_task_ids=set()) == ["t-11"]


async def test_reclaim_startup_removes_stale_dirs(tmp_path, repo_path):
    clock = FakeClock()
    tw = _tw(tmp_path, ttl_seconds=100.0, clock=clock)
    result = await tw.acquire(
        "t-12", [("acme/widgets", str(repo_path))], mode="fresh", branch="ps/t12"
    )
    task_dir = tw.task_dir("t-12")

    # Older than the TTL — a previous process's leftovers we never revisited.
    # Backdated against wall-clock time; see comment in the "fresh dirs" test.
    age = 500.0
    mtime = time.time() - age
    os.utime(task_dir, (mtime, mtime))

    fresh_tw = _tw(tmp_path, ttl_seconds=100.0, clock=clock)
    removed = await fresh_tw.reclaim_startup([("acme/widgets", str(repo_path))])

    assert removed == ["t-12"]
    assert "t-12" not in fresh_tw
    assert not os.path.isdir(task_dir)
    assert not os.path.isdir(_primary(result))


async def test_reclaim_startup_on_missing_base_dir_is_a_noop(tmp_path):
    tw = _tw(tmp_path)
    assert await tw.reclaim_startup([]) == []
