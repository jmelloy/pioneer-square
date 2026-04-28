"""Async git helpers used by the worker."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

EmitFn = Callable[[str], Awaitable[None]]


async def run_git(args: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd or os.getcwd())
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
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
            " ".join(args), rc, elapsed, stderr.decode(errors="replace").strip()[:200],
        )
    else:
        logger.debug("git %s ok in %.2fs", " ".join(args), elapsed)
    return rc, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def ensure_repo(repos_dir: str, repo_full: str, token: Optional[str] = None) -> Optional[str]:
    """Clone repo if absent, otherwise fast-forward to origin/HEAD. Returns local path."""
    parts = repo_full.split("/", 1)
    if len(parts) != 2:
        logger.error("ensure_repo: malformed repo name %r", repo_full)
        return None
    owner, name = parts
    local_path = os.path.join(repos_dir, owner, name)

    remote_url = (
        f"https://{token}@github.com/{repo_full}.git" if token
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


async def remove_worktree(repo_path: str, wt_path: str) -> None:
    logger.info("Removing worktree %s", wt_path)
    await run_git(["worktree", "remove", "--force", wt_path], cwd=repo_path)


async def pull_repos(
    repos_dir: str,
    repos: list[str],
    token: Optional[str],
    emit: EmitFn,
) -> None:
    """Refresh all configured repos; emit progress through *emit*."""
    logger.info("pull_repos: refreshing %d repo(s)", len(repos))
    for repo_full in repos:
        parts = repo_full.split("/", 1)
        if len(parts) != 2:
            logger.warning("pull_repos: skipping malformed repo %r", repo_full)
            continue
        owner, name = parts
        local_path = os.path.join(repos_dir, owner, name)
        if not os.path.exists(os.path.join(local_path, ".git")):
            logger.info("pull_repos: %s missing locally — cloning", repo_full)
            await emit(f"[worker] Cloning {repo_full}...")
            await ensure_repo(repos_dir, repo_full, token)
        else:
            rc, _, err = await run_git(["fetch", "origin"], cwd=local_path)
            await run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)
            if rc == 0:
                logger.info("pull_repos: pulled %s", repo_full)
                await emit(f"[worker] Pulled {repo_full}")
            else:
                logger.warning("pull_repos: fetch warn for %s: %s", repo_full, err.strip()[:120])
                await emit(f"[worker] Pull warn {repo_full}: {err.strip()[:60]}")
