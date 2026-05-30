"""Async git helpers used by the worker."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# emit(line, detail=None, level=...) — accepts an optional ``level`` kwarg that
# types the line for the frontend. Lines emitted here are worker-owned status.
EmitFn = Callable[..., Awaitable[None]]
_LEVEL = "worker"


async def run_git(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd or os.getcwd())
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    rc = proc.returncode if proc.returncode is not None else -1
    elapsed = time.monotonic() - started
    if rc != 0:
        logger.warning(
            "git %s failed rc=%s in %.2fs: %s",
            " ".join(args),
            rc,
            elapsed,
            stderr.decode(errors="replace").strip()[:200],
        )
    else:
        logger.debug("git %s ok in %.2fs", " ".join(args), elapsed)
    return rc, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def ensure_repo(repos_dir: str, repo_full: str, token: str | None = None) -> str | None:
    """Clone repo if absent, otherwise fast-forward to origin/HEAD. Returns local path."""
    parts = repo_full.split("/", 1)
    if len(parts) != 2:
        logger.error("ensure_repo: malformed repo name %r", repo_full)
        return None
    owner, name = parts
    local_path = os.path.join(repos_dir, owner, name)

    remote_url = (
        f"https://{token}@github.com/{repo_full}.git"
        if token
        else f"https://github.com/{repo_full}.git"
    )

    if not os.path.exists(os.path.join(local_path, ".git")):
        logger.info("Cloning %s into %s", repo_full, local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        rc, _, _ = await run_git(["clone", remote_url, local_path])
        if rc != 0:
            logger.error("Clone failed for %s (rc=%d)", repo_full, rc)
            return None
        logger.info("Clone done: %s", repo_full)
    else:
        logger.info("Fetching latest for %s at %s", repo_full, local_path)
        await run_git(["fetch", "origin"], cwd=local_path)
        await run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)

    return local_path


async def create_worktree(repo_path: str, wt_path: str, branch: str) -> bool:
    logger.info("Creating worktree at %s on branch %s (from %s)", wt_path, branch, repo_path)
    os.makedirs(os.path.dirname(wt_path), exist_ok=True)
    await run_git(["fetch", "origin"], cwd=repo_path)
    rc, _, _ = await run_git(
        ["worktree", "add", "-b", branch, wt_path, "origin/HEAD"],
        cwd=repo_path,
    )
    if rc != 0:
        logger.error("Worktree add failed at %s (rc=%d)", wt_path, rc)
    return rc == 0


async def attach_worktree(repo_path: str, wt_path: str, branch: str) -> bool:
    """Create a worktree that checks out an *existing* branch.

    Used when continuing a task on a branch the original worker already
    pushed: a different worker (or the same worker after a worktree-cleanup
    sweep) attaches a fresh worktree to ``origin/<branch>`` so it can keep
    iterating on the same PR.
    """
    logger.info(
        "Attaching worktree at %s to existing branch %s (from %s)", wt_path, branch, repo_path
    )
    os.makedirs(os.path.dirname(wt_path), exist_ok=True)
    await run_git(["fetch", "origin", branch], cwd=repo_path)
    # Prefer attaching to the local branch ref if it exists; otherwise fall
    # back to creating a tracking branch from origin/<branch>.
    rc, _, _ = await run_git(["worktree", "add", wt_path, branch], cwd=repo_path)
    if rc != 0:
        rc, _, _ = await run_git(
            ["worktree", "add", "-B", branch, wt_path, f"origin/{branch}"],
            cwd=repo_path,
        )
    if rc != 0:
        logger.error("Worktree attach failed at %s for branch %s (rc=%d)", wt_path, branch, rc)
    return rc == 0


async def remove_worktree(repo_path: str, wt_path: str) -> None:
    logger.info("Removing worktree %s", wt_path)
    await run_git(["worktree", "remove", "--force", wt_path], cwd=repo_path)


async def prune_worktrees(repo_path: str) -> None:
    """Run ``git worktree prune`` to reclaim references for removed dirs."""
    await run_git(["worktree", "prune"], cwd=repo_path)


async def pull_repos(
    repos_dir: str,
    repos: list[str],
    token: str | None,
    emit: EmitFn,
) -> None:
    """Refresh repos that are already cloned locally; skip repos not yet on disk.

    Repos are cloned lazily the first time a task needs them, so this function
    only fast-forwards existing clones rather than triggering upfront clones.
    """
    logger.info("pull_repos: refreshing %d repo(s)", len(repos))
    for repo_full in repos:
        parts = repo_full.split("/", 1)
        if len(parts) != 2:
            logger.warning("pull_repos: skipping malformed repo %r", repo_full)
            continue
        owner, name = parts
        local_path = os.path.join(repos_dir, owner, name)
        if not os.path.exists(os.path.join(local_path, ".git")):
            logger.debug("pull_repos: %s not cloned yet — will clone on demand", repo_full)
            continue
        rc, _, err = await run_git(["fetch", "origin"], cwd=local_path)
        await run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)
        if rc == 0:
            logger.info("pull_repos: pulled %s", repo_full)
            await emit(f"Pulled {repo_full}", level=_LEVEL)
        else:
            logger.warning("pull_repos: fetch warn for %s: %s", repo_full, err.strip()[:120])
            await emit(f"Pull warn {repo_full}: {err.strip()[:60]}", level=_LEVEL)
