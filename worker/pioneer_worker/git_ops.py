"""Async git helpers used by the worker."""

from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional

EmitFn = Callable[[str], Awaitable[None]]


async def run_git(args: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def ensure_repo(repos_dir: str, repo_full: str, token: Optional[str] = None) -> Optional[str]:
    """Clone repo if absent, otherwise fast-forward to origin/HEAD. Returns local path."""
    parts = repo_full.split("/", 1)
    if len(parts) != 2:
        return None
    owner, name = parts
    local_path = os.path.join(repos_dir, owner, name)

    remote_url = (
        f"https://{token}@github.com/{repo_full}.git" if token
        else f"https://github.com/{repo_full}.git"
    )

    if not os.path.exists(os.path.join(local_path, ".git")):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        rc, _, _ = await run_git(["clone", remote_url, local_path])
        if rc != 0:
            return None
    else:
        await run_git(["fetch", "origin"], cwd=local_path)
        await run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)

    return local_path


async def create_worktree(repo_path: str, wt_path: str, branch: str) -> bool:
    os.makedirs(os.path.dirname(wt_path), exist_ok=True)
    await run_git(["fetch", "origin"], cwd=repo_path)
    rc, _, _ = await run_git(
        ["worktree", "add", "-b", branch, wt_path, "origin/HEAD"],
        cwd=repo_path,
    )
    return rc == 0


async def remove_worktree(repo_path: str, wt_path: str) -> None:
    await run_git(["worktree", "remove", "--force", wt_path], cwd=repo_path)


async def pull_repos(
    repos_dir: str,
    repos: list[str],
    token: Optional[str],
    emit: EmitFn,
) -> None:
    """Refresh all configured repos; emit progress through *emit*."""
    for repo_full in repos:
        parts = repo_full.split("/", 1)
        if len(parts) != 2:
            continue
        owner, name = parts
        local_path = os.path.join(repos_dir, owner, name)
        if not os.path.exists(os.path.join(local_path, ".git")):
            await emit(f"[worker] Cloning {repo_full}...")
            await ensure_repo(repos_dir, repo_full, token)
        else:
            rc, _, err = await run_git(["fetch", "origin"], cwd=local_path)
            await run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)
            if rc == 0:
                await emit(f"[worker] Pulled {repo_full}")
            else:
                await emit(f"[worker] Pull warn {repo_full}: {err.strip()[:60]}")
