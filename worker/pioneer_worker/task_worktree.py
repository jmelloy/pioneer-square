"""Owns the on-disk worktree set materialised for a single task.

``git_ops`` is the shell-out layer (clone, worktree add/remove, ``gh pr
checkout``). This module is the noun sitting on top of it: the layout
convention (one directory per task, one linked worktree per repo inside it),
the reuse heuristic (is there already a worktree here from a prior run on
this task?), the attach-then-create fallback for follow-ups, PR-branch
checkout for review tasks, and the TTL policy that lets worktrees survive
across follow-ups without leaking disk space forever.

``TaskWorktree`` never clones the shared per-repo cache itself — that's a
worker-wide concern (``git_ops.ensure_repo``) independent of any one task.
Callers resolve each repo to a local clone path first and hand ``acquire``
the ``(repo_full, repo_path)`` pairs.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import git_ops

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]
Clock = Callable[[], float]

# Lines emitted here are worker-owned status, same convention as git_ops.EmitFn.
_LEVEL_WORKER = "worker"

# "fresh" always creates a new branch/worktree (aside from same-task reuse).
# "followup" attaches to an existing branch, falling back to "fresh" behaviour
# if the branch never reached origin (e.g. the original run failed pre-push).
# "review_pr" checks out an existing PR's branch via `gh pr checkout` for the
# repo the PR lives in, and behaves like "fresh" for any other repo in the task.
ACQUIRE_MODES = frozenset({"fresh", "followup", "review_pr"})


@dataclass(frozen=True)
class WorktreeEntry:
    repo_full: str
    repo_path: str
    wt_path: str


@dataclass(frozen=True)
class AcquireResult:
    entries: list[WorktreeEntry]
    primary: str | None
    failed: list[str]


def _repo_name(repo_full: str) -> str:
    return repo_full.split("/")[-1]


def _is_worktree_dir(path: str) -> bool:
    """True if *path* looks like a git worktree (linked or plain) checkout."""
    git_marker = os.path.join(path, ".git")
    return os.path.isdir(path) and (os.path.isdir(git_marker) or os.path.isfile(git_marker))


class TaskWorktree:
    """The worktree set materialised for tasks under one worker's work dir.

    Each task gets a subdirectory (named after its task_id) under
    ``base_dir()``, containing one linked worktree per repo the task
    touches. Worktrees outlive a single run so a follow-up can reuse them —
    the registry tracks a last-used timestamp per task, and ``sweep`` /
    ``reclaim_startup`` apply the TTL policy against it.

    ``base_dir`` is a callable rather than a fixed string because a worker's
    identity (and therefore its work directory) can still be pending at
    construction time — self-registered workers only learn their worker_id
    from the backend after startup.

    ``clock`` is injectable so TTL/sweep behaviour is directly testable
    without real time passing; it must return a monotonically increasing
    float (same contract as ``time.monotonic``, which is the default).
    """

    def __init__(
        self,
        base_dir: Callable[[], str],
        *,
        ttl_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self._base_dir = base_dir
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        # task_id -> [(repo_path, wt_path, last_used)]
        self._registry: dict[str, list[tuple[str, str, float]]] = {}

    # ------------------------------------------------------------------ layout
    @property
    def base_dir(self) -> str:
        return self._base_dir()

    def task_dir(self, task_id: str) -> str:
        return os.path.join(self.base_dir, task_id)

    def wt_path(self, task_id: str, repo_full: str) -> str:
        return os.path.join(self.task_dir(task_id), _repo_name(repo_full))

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._registry

    # ------------------------------------------------------------------ acquire
    async def acquire(
        self,
        task_id: str,
        repos: list[tuple[str, str]],
        *,
        mode: str,
        branch: str,
        token: str | None = None,
        pr_repo: str | None = None,
        pr_number: int | str | None = None,
        emit: EmitFn | None = None,
    ) -> AcquireResult:
        """Materialise (or reuse) a worktree per ``(repo_full, repo_path)`` pair.

        ``repos`` must already be cloned locally. Returns every worktree
        that's ready plus the repos that failed; the caller decides whether a
        partial result is fatal (e.g. "no worktrees at all" vs "one repo of
        three failed").
        """
        if mode not in ACQUIRE_MODES:
            raise ValueError(f"unknown TaskWorktree mode: {mode!r}")

        os.makedirs(self.task_dir(task_id), exist_ok=True)
        entries: list[WorktreeEntry] = []
        failed: list[str] = []

        for repo_full, repo_path in repos:
            wt = self.wt_path(task_id, repo_full)
            repo_name = _repo_name(repo_full)

            if _is_worktree_dir(wt):
                # Worktree from a prior run on the same task — reuse it.
                logger.info("Task %s: reusing worktree at %s", task_id, wt)
                if emit:
                    await emit(f"Reusing worktree {repo_name}", level=_LEVEL_WORKER)
                if mode in ("followup", "review_pr"):
                    # Pull latest so we don't clobber commits pushed since the
                    # last follow-up or by other workers.
                    await git_ops.run_git(["fetch", "origin", branch], cwd=wt, token=token)
                    await git_ops.run_git(["reset", "--hard", f"origin/{branch}"], cwd=wt)
                entries.append(WorktreeEntry(repo_full, repo_path, wt))
                continue

            if mode == "followup":
                logger.info("Task %s: attaching worktree %s to branch %s", task_id, wt, branch)
                # attach_worktree fetches origin/<branch> before checking it out,
                # so the new worktree starts at the latest remote commit.
                ok = await git_ops.attach_worktree(repo_path, wt, branch, token)
                if not ok:
                    logger.warning(
                        "Task %s: attach failed for %s — branch %s not found on origin; "
                        "falling back to create_worktree",
                        task_id,
                        repo_full,
                        branch,
                    )
                    if emit:
                        await emit(
                            f"Branch not found on origin; creating fresh branch {branch[:50]}",
                            level=_LEVEL_WORKER,
                        )
                    ok = await git_ops.create_worktree(repo_path, wt, branch, token)
            elif mode == "review_pr" and pr_repo and repo_full.lower() == pr_repo.lower():
                # Check out the PR's own branch via `gh pr checkout` instead of
                # `git checkout -b` — review tasks read an existing PR, they
                # never create a new branch.
                logger.info(
                    "Task %s: checking out PR #%s (%s) into worktree %s",
                    task_id,
                    pr_number,
                    repo_full,
                    wt,
                )
                ok = await git_ops.checkout_pr_worktree(repo_path, wt, pr_number, repo_full, token)
            else:
                if mode == "review_pr":
                    logger.warning(
                        "Task %s: repo %s does not match PR repo %s (case-insensitive) — "
                        "falling back to create_worktree instead of gh pr checkout",
                        task_id,
                        repo_full,
                        pr_repo,
                    )
                logger.info("Task %s: creating worktree %s on branch %s", task_id, wt, branch)
                ok = await git_ops.create_worktree(repo_path, wt, branch, token)

            if ok:
                logger.info("Task %s: worktree ready at %s", task_id, wt)
                if emit:
                    await emit(f"Worktree ready: {repo_name}", level=_LEVEL_WORKER)
                entries.append(WorktreeEntry(repo_full, repo_path, wt))
            else:
                logger.error("Task %s: worktree failed for %s", task_id, repo_full)
                if emit:
                    await emit(f"✗ Worktree failed: {repo_full}", level=_LEVEL_WORKER)
                failed.append(repo_full)

        if entries:
            self._register(task_id, entries)

        return AcquireResult(
            entries=entries,
            primary=entries[0].wt_path if entries else None,
            failed=failed,
        )

    # ------------------------------------------------------------------ registry / TTL
    def _register(self, task_id: str, entries: list[WorktreeEntry]) -> None:
        ts = self._clock()
        self._registry[task_id] = [(e.repo_path, e.wt_path, ts) for e in entries]

    def touch(self, task_id: str) -> None:
        """Refresh the activity timestamp for a task's worktrees."""
        existing = self._registry.get(task_id)
        if not existing:
            return
        ts = self._clock()
        self._registry[task_id] = [(rp, wt, ts) for rp, wt, _old in existing]

    async def release(self, task_id: str) -> None:
        """Remove all worktrees for a task immediately and forget them."""
        entries = self._registry.pop(task_id, None)
        if not entries:
            return
        logger.info("Releasing %d worktree(s) for task %s", len(entries), task_id)
        for repo_path, wt_path, _ts in entries:
            try:
                await git_ops.remove_worktree(repo_path, wt_path)
            except Exception as exc:
                logger.warning("remove_worktree failed for %s: %s", wt_path, exc)

    def stale_task_ids(self, active_task_ids: set[str]) -> list[str]:
        """Task ids whose worktrees are all past the TTL and not in-flight."""
        now = self._clock()
        stale = []
        for task_id, entries in self._registry.items():
            if task_id in active_task_ids:
                continue
            if all(now - ts > self.ttl_seconds for _rp, _wt, ts in entries):
                stale.append(task_id)
        return stale

    async def sweep(self, active_task_ids: set[str]) -> list[str]:
        """Release every task past the TTL that isn't currently running.

        Returns the task ids that were released.
        """
        stale = self.stale_task_ids(active_task_ids)
        for task_id in stale:
            await self.release(task_id)
        return stale

    # ------------------------------------------------------------------ startup reclamation
    async def reclaim_startup(self, known_repos: list[tuple[str, str]]) -> list[str]:
        """Reconcile in-memory state with whatever this worker left on disk.

        A previous process may have died mid-task, so nothing in memory
        tracks its worktrees. Walk ``base_dir`` and either re-register a task
        dir (if still within the TTL, so the deferred sweeper takes over) or
        remove it outright. ``known_repos`` is the ``(repo_full, repo_path)``
        pairs this worker can see, used to map each per-task checkout back to
        the shared repo clone it was made from. Returns the task ids removed
        outright.
        """
        removed: list[str] = []
        base = self.base_dir
        if not os.path.isdir(base):
            return removed

        wall_now = datetime.now(UTC).timestamp()
        for entry in os.listdir(base):
            full = os.path.join(base, entry)
            if not os.path.isdir(full):
                continue
            try:
                age = wall_now - os.path.getmtime(full)
            except OSError:
                continue

            if age <= self.ttl_seconds:
                ts = self._clock() - age
                repos_for_task = [
                    (repo_path, wt)
                    for repo_full, repo_path in known_repos
                    if os.path.isdir(wt := os.path.join(full, _repo_name(repo_full)))
                ]
                if repos_for_task:
                    self._registry[entry] = [(rp, wt, ts) for rp, wt in repos_for_task]
                continue

            logger.info("Startup sweep: removing stale task dir %s (age=%.0fs)", full, age)
            for repo_full, repo_path in known_repos:
                wt = os.path.join(full, _repo_name(repo_full))
                if os.path.isdir(wt):
                    try:
                        await git_ops.remove_worktree(repo_path, wt)
                    except Exception as exc:
                        logger.debug("startup-sweep remove_worktree failed: %s", exc)
            try:
                shutil.rmtree(full, ignore_errors=True)
            except Exception as exc:
                logger.debug("startup-sweep rmtree failed for %s: %s", full, exc)
            removed.append(entry)

        for _repo_full, repo_path in known_repos:
            if os.path.isdir(repo_path):
                try:
                    await git_ops.prune_worktrees(repo_path)
                except Exception as exc:
                    logger.debug("prune_worktrees failed for %s: %s", repo_path, exc)

        return removed
