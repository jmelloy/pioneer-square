"""Push a branch and open a GitHub pull request."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Awaitable, Callable, Optional

from . import git_ops

EmitFn = Callable[[str], Awaitable[None]]


async def push_branch(
    *,
    branch: str,
    worktree_path: str,
    emit: EmitFn,
) -> bool:
    """Push *branch* to origin. Returns True on success."""
    # Stage and commit any uncommitted work so it isn't silently lost on push
    # (can happen on max-turns, plan-phase completion, or normal task end).
    rc_status, status_out, _ = await git_ops.run_git(["status", "--porcelain"], cwd=worktree_path)
    if rc_status == 0 and status_out.strip():
        await emit("[worker] Uncommitted changes detected — auto-committing before push")
        await git_ops.run_git(["add", "-A"], cwd=worktree_path)
        rc_commit, _, commit_err = await git_ops.run_git(
            ["commit", "-m", "chore: save uncommitted work before push [auto-commit]"],
            cwd=worktree_path,
        )
        if rc_commit != 0:
            await emit(f"[worker] ✗ Auto-commit failed: {commit_err.strip()[:120]}")
            return False

    await emit(f"[worker] Pushing {branch}...")
    rc, _, err = await git_ops.run_git(["push", "-u", "origin", branch], cwd=worktree_path)
    if rc != 0:
        await emit(f"[worker] ✗ Push failed: {err.strip()[:120]}")
        return False
    await emit(f"[worker] ✓ Pushed {branch}")
    return True


async def open_pr(
    *,
    task: dict,
    branch: str,
    worktree_path: str,
    token: Optional[str],
    emit: EmitFn,
) -> Optional[str]:
    """Create a GitHub PR for *branch*. Returns PR URL or None on failure."""
    if not token:
        await emit("[worker] No GitHub token — skipping PR")
        return None

    repo_full = task.get("issue_repo")
    if not repo_full:
        rc2, url, _ = await git_ops.run_git(["remote", "get-url", "origin"], cwd=worktree_path)
        if rc2 == 0:
            m = re.search(r"github\.com[:/](.+?/[^/\s]+?)(?:\.git)?$", url.strip())
            if m:
                repo_full = m.group(1)
    if not repo_full:
        await emit("[worker] Could not determine repo — skipping PR")
        return None

    issue_ref = f"\n\nCloses #{task['issue_number']}" if task.get("issue_number") else ""
    body = f"Automated by Pioneer Square worker agent.{issue_ref}"
    payload = json.dumps({
        "title": (task.get("description") or "")[:72],
        "body": body,
        "head": branch,
        "base": "main",
    }).encode()

    def _create_pr() -> dict:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo_full}/pulls",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    try:
        result = await asyncio.to_thread(_create_pr)
        pr_url = result.get("html_url", "")
        await emit(f"[worker] ✓ PR: {pr_url}")
        return pr_url
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        await emit(f"[worker] PR failed: {exc}")
        return None
