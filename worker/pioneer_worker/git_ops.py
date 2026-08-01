"""Async git helpers used by the worker."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# emit(line, detail=None, level=...) — accepts an optional ``level`` kwarg that
# types the line for the frontend. Lines emitted here are worker-owned status.
EmitFn = Callable[..., Awaitable[None]]
_LEVEL = "worker"

# Matches a remote URL carrying inline credentials (https://<userinfo>@host/…).
_CREDENTIALED_URL_RE = re.compile(r"^(https?://)[^/@]+@")


def _auth_env(token: str | None) -> dict[str, str] | None:
    """Per-invocation git config carrying *token*, or None when there is none.

    The token travels as an ``http.extraHeader`` supplied through git's
    ``GIT_CONFIG_*`` environment protocol, which is scoped to the single git
    process: it is never written to the repo's config (as a credentialed clone
    URL would be) and never appears in argv (as an authenticated push URL
    does). The header is scoped to github.com so it can't ride along on a
    redirect to another host.
    """
    if not token:
        return None
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


async def run_git(
    args: list[str], cwd: str | None = None, token: str | None = None
) -> tuple[int, str, str]:
    """Run a git command. *token*, when given, authenticates this call only."""
    logger.debug("git %s (cwd=%s)", " ".join(args), cwd or os.getcwd())
    started = time.monotonic()
    auth = _auth_env(token)
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **auth} if auth else None,
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


async def scrub_remote_credentials(repo_path: str) -> bool:
    """Rewrite ``origin`` to a credential-free URL if it carries inline userinfo.

    Clones made before credentials moved to a per-invocation header persisted
    ``https://<token>@github.com/…`` in ``.git/config``. Those clones keep using
    that one token for every later fetch no matter who the fetch is for, so the
    URL is rewritten in place the first time we touch such a repo. Returns True
    when a rewrite happened.
    """
    # `config --get` returns the literal stored value; `remote get-url` would
    # apply any insteadOf rewrite, and writing that back would replace the
    # canonical URL with a rewritten one.
    rc, url, _ = await run_git(["config", "--get", "remote.origin.url"], cwd=repo_path)
    if rc != 0:
        return False
    url = url.strip()
    if not _CREDENTIALED_URL_RE.match(url):
        return False
    clean = _CREDENTIALED_URL_RE.sub(r"\1", url)
    rc, _, _ = await run_git(["remote", "set-url", "origin", clean], cwd=repo_path)
    if rc == 0:
        # Log the cleaned URL only — the original one contains the credential.
        logger.warning(
            "Removed embedded credentials from origin URL of %s (now %s)", repo_path, clean
        )
        return True
    return False


async def ensure_repo(repos_dir: str, repo_full: str, token: str | None = None) -> str | None:
    """Clone repo if absent, otherwise fast-forward to origin/HEAD. Returns local path.

    *token* authenticates this call only. It is deliberately NOT baked into the
    ``origin`` URL: a persisted credential is used by every later fetch on this
    shared clone regardless of which user the fetch is for, so the first caller's
    token would silently serve everyone else's tasks — and outlive its own
    validity.
    """
    parts = repo_full.split("/", 1)
    if len(parts) != 2:
        logger.error("ensure_repo: malformed repo name %r", repo_full)
        return None
    owner, name = parts
    local_path = os.path.join(repos_dir, owner, name)

    remote_url = f"https://github.com/{repo_full}.git"

    if not os.path.exists(os.path.join(local_path, ".git")):
        logger.info("Cloning %s into %s", repo_full, local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        rc, _, _ = await run_git(["clone", remote_url, local_path], token=token)
        if rc != 0:
            logger.error("Clone failed for %s (rc=%d)", repo_full, rc)
            return None
        logger.info("Clone done: %s", repo_full)
    else:
        await scrub_remote_credentials(local_path)
        logger.info("Fetching latest for %s at %s", repo_full, local_path)
        await run_git(["fetch", "origin"], cwd=local_path, token=token)
        await run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)

    return local_path


async def create_worktree(
    repo_path: str, wt_path: str, branch: str, token: str | None = None
) -> bool:
    logger.info("Creating worktree at %s on branch %s (from %s)", wt_path, branch, repo_path)
    os.makedirs(os.path.dirname(wt_path), exist_ok=True)
    await run_git(["fetch", "origin"], cwd=repo_path, token=token)
    rc, _, _ = await run_git(
        ["worktree", "add", "-b", branch, wt_path, "origin/HEAD"],
        cwd=repo_path,
    )
    if rc != 0:
        logger.error("Worktree add failed at %s (rc=%d)", wt_path, rc)
    return rc == 0


async def attach_worktree(
    repo_path: str, wt_path: str, branch: str, token: str | None = None
) -> bool:
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
    await run_git(["fetch", "origin", branch], cwd=repo_path, token=token)
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


async def set_git_identity(name: str, email: str) -> None:
    """Set the global git author/committer identity for this worker process.

    Used so Claude's commits are attributed to the GitHub App bot: with the
    bot's ``<id>+<slug>@users.noreply.github.com`` email, GitHub links commits
    to the App identity rather than whichever token authenticated the push.
    """
    await run_git(["config", "--global", "user.name", name])
    await run_git(["config", "--global", "user.email", email])


async def run_gh(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    logger.debug("gh %s (cwd=%s)", " ".join(args), cwd or os.getcwd())
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    rc = proc.returncode if proc.returncode is not None else -1
    if rc != 0:
        logger.warning(
            "gh %s failed rc=%s: %s",
            " ".join(args),
            rc,
            stderr.decode(errors="replace").strip()[:200],
        )
    return rc, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def get_pr_head_branch(repo_full: str, pr_number: int | str) -> str | None:
    """Look up a PR's head branch name via the ``gh`` CLI. Returns None on failure."""
    rc, out, _ = await run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo_full,
            "--json",
            "headRefName",
            "-q",
            ".headRefName",
        ]
    )
    if rc != 0:
        return None
    branch = out.strip()
    return branch or None


async def checkout_pr_worktree(
    repo_path: str, wt_path: str, pr_number: int | str, repo_full: str, token: str | None = None
) -> bool:
    """Create a worktree checked out to an existing PR's branch via ``gh pr checkout``.

    Used for review-phase tasks: the worker must review the PR's actual branch
    rather than a freshly generated one. A detached worktree is created first
    (linked worktrees can't share a branch with the main checkout), then
    ``gh pr checkout`` is run inside it so `gh` resolves the head branch
    (including cross-fork PRs) and checks it out in place.
    """
    logger.info("Checking out PR #%s (%s) into worktree %s", pr_number, repo_full, wt_path)
    if parent := os.path.dirname(wt_path):
        os.makedirs(parent, exist_ok=True)
    await run_git(["fetch", "origin"], cwd=repo_path, token=token)
    rc, _, _ = await run_git(["worktree", "add", "--detach", wt_path, "origin/HEAD"], cwd=repo_path)
    if rc != 0:
        logger.error("Detached worktree add failed at %s (rc=%d)", wt_path, rc)
        return False
    rc, _, _ = await run_gh(["pr", "checkout", str(pr_number), "--repo", repo_full], cwd=wt_path)
    if rc != 0:
        logger.error("gh pr checkout failed for PR #%s (%s) in %s", pr_number, repo_full, wt_path)
        # Detached worktree add succeeded but the checkout didn't — remove the
        # orphaned worktree so it doesn't linger as a dangling checkout.
        await run_git(["worktree", "remove", "--force", wt_path], cwd=repo_path)
        return False
    return True


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
        await scrub_remote_credentials(local_path)
        rc, _, err = await run_git(["fetch", "origin"], cwd=local_path, token=token)
        await run_git(["merge", "--ff-only", "origin/HEAD"], cwd=local_path)
        if rc == 0:
            logger.info("pull_repos: pulled %s", repo_full)
            await emit(f"Pulled {repo_full}", level=_LEVEL)
        else:
            logger.warning("pull_repos: fetch warn for %s: %s", repo_full, err.strip()[:120])
            await emit(f"Pull warn {repo_full}: {err.strip()[:60]}", level=_LEVEL)
