"""Push a branch and open a GitHub pull request."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable

from . import git_ops

EmitFn = Callable[[str], Awaitable[None]]

logger = logging.getLogger(__name__)


# GitHub webhook events the foreman cares about — kept narrow so a single
# repo's hook doesn't fan out the firehose. ``status`` is the legacy
# pre-checks API; we subscribe so older repos still surface CI signals.
_WEBHOOK_EVENTS = [
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
    "check_run",
    "status",
]


async def fetch_accessible_repos(token: str) -> list[str]:
    """Return repos accessible to the GitHub token via the API.

    Queries /user/repos with pagination (up to 200 repos). Returns
    ``["owner/repo", ...]``. Network errors are logged; an empty list is
    returned so callers can treat a failed fetch as a no-op.
    """

    def _fetch_page(page: int) -> list:
        req = urllib.request.Request(
            f"https://api.github.com/user/repos?per_page=100&page={page}&sort=pushed",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    all_repos: list[str] = []
    for page in range(1, 3):  # cap at 200 repos (2 pages × 100)
        try:
            repos = await asyncio.to_thread(_fetch_page, page)
        except Exception as exc:
            logger.warning("GitHub repo list fetch failed (page %d): %s", page, exc)
            break
        if not repos:
            break
        all_repos.extend(r["full_name"] for r in repos if r.get("full_name"))
        if len(repos) < 100:
            break
    return all_repos


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


async def find_existing_pr(
    *,
    branch: str,
    worktree_path: str,
    token: str | None,
) -> str | None:
    """Return the HTML URL of an open PR for *branch*, or None."""
    if not token:
        return None

    repo_full = None
    rc, url, _ = await git_ops.run_git(["remote", "get-url", "origin"], cwd=worktree_path)
    if rc == 0:
        m = re.search(r"github\.com[:/](.+?/[^/\s]+?)(?:\.git)?$", url.strip())
        if m:
            repo_full = m.group(1)
    if not repo_full:
        return None

    owner = repo_full.split("/")[0]

    def _list_prs() -> list:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo_full}/pulls?head={owner}:{branch}&state=open",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    try:
        pulls = await asyncio.to_thread(_list_prs)
        if pulls:
            return pulls[0].get("html_url")
    except Exception:
        pass
    return None


async def open_pr(
    *,
    task: dict,
    branch: str,
    worktree_path: str,
    token: str | None,
    emit: EmitFn,
) -> str | None:
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

    task_name = task.get("name") or task.get("description") or ""
    task_id = task.get("id") or ""
    closes_line = f"\n\nCloses #{task['issue_number']}" if task.get("issue_number") else ""
    body = (
        f"**Task:** {task_name}\n"
        f"**Task ID:** `{task_id}`\n\n"
        f"Automated by Pioneer Square worker agent.{closes_line}"
    )
    payload = json.dumps(
        {
            "title": task_name[:72] or (task.get("description") or "")[:72],
            "body": body,
            "head": branch,
            "base": "main",
        }
    ).encode()

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
    except urllib.error.HTTPError as exc:
        if exc.fp is not None:
            exc.fp.close()
        if exc.code == 422:
            await emit(f"[worker] PR creation returned 422 — checking for existing PR on {branch}")
            existing = await find_existing_pr(
                branch=branch, worktree_path=worktree_path, token=token
            )
            if existing:
                await emit(f"[worker] ✓ Found existing PR: {existing}")
                return existing
            await emit(f"[worker] PR failed: {exc}")
            return None
        logger.error("PR creation failed: %s", exc)
        raise
    except (urllib.error.URLError, OSError) as exc:
        await emit(f"[worker] PR failed: {exc}")
        return None


async def _fetch_webhook_secret(*, http_url: str, guild_id: str, auth_token: str) -> str | None:
    """GET the guild's webhook secret from the backend, returning None on failure.

    The backend lazily generates the secret on first read (see
    ``backend/routes/guilds.py``), so the first PR ever opened against a guild
    transparently provisions one.
    """

    def _get() -> dict:
        req = urllib.request.Request(
            f"{http_url.rstrip('/')}/guilds/{guild_id}/webhook-secret",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    try:
        data = await asyncio.to_thread(_get)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        logger.warning("webhook secret fetch failed: %s", exc)
        return None
    secret = data.get("webhook_secret") if isinstance(data, dict) else None
    return secret if isinstance(secret, str) and secret else None


def _repo_from_pr_url(pr_url: str) -> str | None:
    """Extract ``owner/repo`` from a PR HTML URL like
    ``https://github.com/owner/repo/pull/42``."""
    if not pr_url:
        return None
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/\d+", pr_url)
    return m.group(1) if m else None


async def ensure_webhook(
    *,
    repo: str,
    target_url: str,
    http_url: str,
    guild_id: str,
    auth_token: str | None,
    github_token: str | None,
    emit: EmitFn,
) -> bool:
    """Idempotently configure a Pioneer Square webhook on *repo*.

    Best-effort: returns True when the hook is in place after this call,
    False on any failure. Failures emit a single warning and are otherwise
    swallowed — the PR has already been opened, so missing webhook coverage
    shouldn't block the task.

    Idempotency: looks up existing hooks first. If one already targets
    *target_url* it's reused (and updated only when the active flag, secret,
    or events list drift). Otherwise a new hook is created.
    """
    if not auth_token:
        await emit("[worker] webhook setup skipped — no worker auth_token")
        return False
    if not github_token:
        await emit("[worker] webhook setup skipped — no GitHub token")
        return False

    secret = await _fetch_webhook_secret(
        http_url=http_url, guild_id=guild_id, auth_token=auth_token
    )
    if not secret:
        await emit("[worker] webhook setup skipped — backend did not return a secret")
        return False

    def _list_hooks() -> list:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/hooks?per_page=100",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def _create_hook() -> dict:
        body = json.dumps(
            {
                "name": "web",
                "active": True,
                "events": _WEBHOOK_EVENTS,
                "config": {
                    "url": target_url,
                    "secret": secret,
                    "content_type": "json",
                    "insecure_ssl": "0",
                },
            }
        ).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/hooks",
            data=body,
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def _patch_hook(hook_id: int) -> dict:
        body = json.dumps(
            {
                "active": True,
                "events": _WEBHOOK_EVENTS,
                "config": {
                    "url": target_url,
                    "secret": secret,
                    "content_type": "json",
                    "insecure_ssl": "0",
                },
            }
        ).encode()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/hooks/{hook_id}",
            data=body,
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    try:
        hooks = await asyncio.to_thread(_list_hooks)
    except urllib.error.HTTPError as exc:
        # 403 => token lacks admin:repo_hook (or admin access on the repo).
        # Treat as a non-fatal warning; the operator can configure manually.
        await emit(f"[worker] webhook list failed ({exc.code}): {exc.reason}")
        return False
    except (urllib.error.URLError, OSError) as exc:
        await emit(f"[worker] webhook list failed: {exc}")
        return False

    existing = next(
        (
            h
            for h in hooks
            if isinstance(h, dict) and (h.get("config") or {}).get("url") == target_url
        ),
        None,
    )

    try:
        if existing:
            # Always re-PATCH: the secret may have been rotated, the events
            # list may have grown, or the hook may have been deactivated.
            await asyncio.to_thread(_patch_hook, int(existing["id"]))
            await emit(f"[worker] ✓ Webhook refreshed on {repo}")
        else:
            await asyncio.to_thread(_create_hook)
            await emit(f"[worker] ✓ Webhook installed on {repo} → {target_url}")
        return True
    except urllib.error.HTTPError as exc:
        await emit(f"[worker] webhook setup failed ({exc.code}): {exc.reason}")
        return False
    except (urllib.error.URLError, OSError) as exc:
        await emit(f"[worker] webhook setup failed: {exc}")
        return False
