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
# repo the PR lives in; supporting repos use detached default-branch checkouts.
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

    ``base_dir`` is a callable so the directory is resolved lazily. It must
    not embed the worker_id, so a task's worktree is reusable across workers.

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
        legacy = os.path.join(self.task_dir(task_id), _repo_name(repo_full))
        if os.path.isdir(legacy):
            return legacy
        return f"{legacy}-{task_id}"

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

            is_pr_repo = mode == "review_pr" and pr_repo and repo_full.lower() == pr_repo.lower()
            if _is_worktree_dir(wt):
                rc, common_dir, _ = await git_ops.run_git(
                    ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=wt
                )
                repo_rc, repo_common_dir, _ = await git_ops.run_git(
                    ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo_path
                )
                branch_rc, current_branch, _ = await git_ops.run_git(
                    ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=wt
                )
                if (
                    rc != 0
                    or repo_rc != 0
                    or os.path.realpath(common_dir.strip())
                    != os.path.realpath(repo_common_dir.strip())
                    or branch_rc not in (0, 1)
                    or (branch_rc == 0 and current_branch.strip() != branch)
                ):
                    logger.error(
                        "Task %s: wrong repository or branch at %s; leaving untouched", task_id, wt
                    )
                    if emit:
                        await emit(
                            f"✗ Wrong repository or branch at {wt}; keeping local state",
                            level=_LEVEL_WORKER,
                        )
                    failed.append(repo_full)
                    continue
                # Worktree from a prior run on the same task — reuse it.
                logger.info("Task %s: reusing worktree at %s", task_id, wt)
                if emit:
                    await emit(f"Reusing worktree {repo_name}", level=_LEVEL_WORKER)
                if mode == "followup" or is_pr_repo:
                    # Retained worktrees may contain unpublished edits; fail closed.
                    rc, status, _ = await git_ops.run_git(["status", "--porcelain"], cwd=wt)
                    if rc != 0 or status.strip():
                        logger.warning(
                            "Task %s: worktree %s is dirty or status failed; "
                            "skipping reset to origin/%s",
                            task_id,
                            wt,
                            branch,
                        )
                        if emit:
                            await emit(
                                f"Worktree {repo_name} is dirty or status failed; "
                                "keeping local state (not reset to origin)",
                                level=_LEVEL_WORKER,
                            )
                    else:
                        # GitHub exposes fork PR heads on the base repo's pull refs too.
                        remote_branch = (
                            f"refs/pull/{pr_number}/head" if is_pr_repo else f"refs/heads/{branch}"
                        )
                        local_ref = (
                            f"refs/pioneer/pr/{pr_number}"
                            if is_pr_repo
                            else f"refs/remotes/origin/{branch}"
                        )
                        fetched, _, _ = await git_ops.run_git(
                            ["fetch", "origin", f"+{remote_branch}:{local_ref}"],
                            cwd=wt,
                            token=token,
                        )
                        if fetched != 0:
                            # Local-only branches and offline recovery must retain their work.
                            if is_pr_repo:
                                failed.append(repo_full)
                                continue
                            if emit:
                                await emit(
                                    f"Could not refresh {repo_name}; keeping local state",
                                    level=_LEVEL_WORKER,
                                )
                        else:
                            rc, target, _ = await git_ops.run_git(
                                ["rev-parse", "--verify", local_ref], cwd=wt
                            )
                            if rc != 0:
                                failed.append(repo_full)
                                continue
                            rc, _, _ = await git_ops.run_git(
                                ["merge-base", "--is-ancestor", "HEAD", target.strip()], cwd=wt
                            )
                            if rc == 0:
                                rc, _, _ = await git_ops.run_git(
                                    ["reset", "--hard", target.strip()], cwd=wt
                                )
                            elif rc == 1:
                                # A clean index does not mean the local commits were published.
                                if emit:
                                    await emit(
                                        f"Unpublished or divergent commits in {repo_name}; keeping local state",
                                        level=_LEVEL_WORKER,
                                    )
                                rc = 0
                            if rc != 0:
                                failed.append(repo_full)
                                continue
                entries.append(WorktreeEntry(repo_full, repo_path, wt))
                continue

            if mode == "followup":
                logger.info("Task %s: attaching worktree %s to branch %s", task_id, wt, branch)
                # Start at the fetched head unless there are unpublished local commits.
                ok = await git_ops.attach_worktree(repo_path, wt, branch, token)
                if not ok:
                    missing, _, _ = await git_ops.run_git(
                        ["ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
                        cwd=repo_path,
                        token=token,
                    )
                    # Only a confirmed missing branch permits starting from the default head.
                    if missing == 2:
                        logger.warning(
                            "Task %s: branch %s never reached origin; creating worktree",
                            task_id,
                            branch,
                        )
                        if emit:
                            await emit(
                                f"Branch not found on origin; creating fresh branch {branch[:50]}",
                                level=_LEVEL_WORKER,
                            )
                        ok = await git_ops.create_worktree(repo_path, wt, branch, token)
            elif is_pr_repo:
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
                    rc, _, _ = await git_ops.run_git(
                        ["worktree", "add", "--detach", wt, "origin/HEAD"], cwd=repo_path
                    )
                    ok = rc == 0
                else:
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

    async def finalize(self, task_id: str, *, branch: str, token: str | None = None) -> bool:
        """Remove only clean, published worktrees; keep recovery state on errors."""
        entries = self._registry.get(task_id)
        if not entries or not branch:
            return False
        try:
            # Check every repo before removing any of a multi-repo task's worktrees.
            for _repo_path, wt_path, _ts in entries:
                rc, status, err = await git_ops.run_git(["status", "--porcelain"], cwd=wt_path)
                if rc != 0:
                    raise RuntimeError(err or "git status failed")
                if status.strip():
                    logger.info("Task %s: retaining dirty worktree %s", task_id, wt_path)
                    return False
                # Publishing uses HEAD:<branch>, including detached checkouts, without
                # setting @{u}. Ask origin directly rather than trusting cached refs.
                rc, remote, err = await git_ops.run_git(
                    ["ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"],
                    cwd=wt_path,
                    token=token,
                )
                if rc != 0 or not remote.strip():
                    raise RuntimeError(err or "remote branch unavailable")
                remote_head = remote.split()[0]
                rc, unpushed, err = await git_ops.run_git(
                    ["log", "--oneline", f"{remote_head}..HEAD"], cwd=wt_path
                )
                if rc != 0:
                    raise RuntimeError(err or "could not verify published commits")
                if unpushed.strip():
                    logger.info("Task %s: retaining unpublished worktree %s", task_id, wt_path)
                    return False
            for repo_path, wt_path, _ts in entries:
                # Never force removal: Git must refuse dirty or locked worktrees.
                rc, _, err = await git_ops.run_git(["worktree", "remove", wt_path], cwd=repo_path)
                if rc != 0:
                    raise RuntimeError(err or f"could not remove {wt_path}")
            self._registry.pop(task_id, None)
            # Task-level artifacts may still occupy the parent directory.
            try:
                os.rmdir(self.task_dir(task_id))
            except OSError:
                pass
            logger.info("Task %s: removed clean, published worktrees", task_id)
            return True
        except Exception:
            logger.warning(
                "Task %s: worktree cleanup failed; retaining recovery state", task_id, exc_info=True
            )
            return False

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
                    if os.path.isdir(wt := self.wt_path(entry, repo_full))
                ]
                if repos_for_task:
                    self._registry[entry] = [(rp, wt, ts) for rp, wt in repos_for_task]
                continue

            logger.info("Startup sweep: removing stale task dir %s (age=%.0fs)", full, age)
            for repo_full, repo_path in known_repos:
                wt = self.wt_path(entry, repo_full)
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
