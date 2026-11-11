"""Publish one working-tree checkpoint as a verified GitHub App commit."""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from . import git_ops

logger = logging.getLogger(__name__)

# GitHub's committer for commits it signs on behalf of API callers.
WEB_FLOW = "web-flow"


class PublishError(Exception):
    """Publication failed; local work must be preserved."""


def _pair(name: str | None, email: str | None) -> tuple[str, str] | None:
    return (name, email) if name and email else None


def _api_identity(pair: tuple[str, str] | None) -> dict | None:
    if not pair:
        return None
    return {"name": pair[0], "email": pair[1], "date": datetime.now(UTC).isoformat()}


def commit_env(context: PublishContext) -> dict[str, str]:
    """GIT_AUTHOR_*/GIT_COMMITTER_* for the author (user) + committer (bot) pair."""
    env: dict[str, str] = {}
    for prefix, pair in (("AUTHOR", context.author), ("COMMITTER", context.committer)):
        if pair:
            env[f"GIT_{prefix}_NAME"], env[f"GIT_{prefix}_EMAIL"] = pair
    return env


class SigningUnavailable(PublishError):
    """App signing is unavailable or unverified; unsigned publication is allowed."""


@dataclass(frozen=True)
class PublishContext:
    backend_url: str
    guild_id: str
    task_id: str
    auth_token: str = field(repr=False)
    branch: str
    # Commit identity: ``author`` is the human who triggered the task and
    # ``committer`` is the GitHub App bot, each a (name, email) pair or None.
    author: tuple[str, str] | None = None
    committer: tuple[str, str] | None = None

    @classmethod
    def from_env(cls) -> PublishContext:
        keys = (
            "PIONEER_BACKEND_URL",
            "PIONEER_GUILD_ID",
            "PIONEER_TASK_ID",
            "PIONEER_AUTH_TOKEN",
            "PIONEER_PUBLISH_BRANCH",
        )
        if any(not os.environ.get(key) for key in keys):
            raise PublishError(
                "Missing PIONEER_ task credentials; run inside an assigned worker task"
            )
        values = [os.environ[key] for key in keys]
        values[0] = values[0].replace("wss://", "https://").replace("ws://", "http://")
        return cls(
            *values,
            author=_pair(
                os.environ.get("PIONEER_AUTHOR_NAME"), os.environ.get("PIONEER_AUTHOR_EMAIL")
            ),
            committer=_pair(
                os.environ.get("PIONEER_COMMITTER_NAME"), os.environ.get("PIONEER_COMMITTER_EMAIL")
            ),
        )

    def environment(self) -> dict[str, str]:
        env = self._base_environment()
        for prefix, pair in (("AUTHOR", self.author), ("COMMITTER", self.committer)):
            if pair:
                env[f"PIONEER_{prefix}_NAME"], env[f"PIONEER_{prefix}_EMAIL"] = pair
        return env

    def _base_environment(self) -> dict[str, str]:
        return dict(
            zip(
                (
                    "PIONEER_BACKEND_URL",
                    "PIONEER_GUILD_ID",
                    "PIONEER_TASK_ID",
                    "PIONEER_AUTH_TOKEN",
                    "PIONEER_PUBLISH_BRANCH",
                ),
                (self.backend_url, self.guild_id, self.task_id, self.auth_token, self.branch),
                strict=True,
            )
        )


async def _git(
    args: list[str],
    *,
    cwd: Path,
    token: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> bytes:
    auth = {**(git_ops._auth_env(token) or {}), **(extra_env or {})}
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env={**os.environ, **auth} if auth else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await proc.communicate()
    if proc.returncode:
        raise PublishError(f"Git {args[0]} failed; inspect the worktree before retrying")
    return out


async def _repository(cwd: Path) -> str:
    url = (await _git(["config", "--get", "remote.origin.url"], cwd=cwd)).decode().strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url
    )
    if not match:
        raise PublishError("Publication requires a credential-free github.com origin")
    return match[1].removesuffix(".git")


async def _json(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    return _response_json(await client.request(method, path, **kwargs))


def _response_json(response: httpx.Response) -> dict:
    if response.is_error or response.is_redirect:
        raise PublishError(
            f"Publication request failed (HTTP {response.status_code}); check App access"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise PublishError("Publication service returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise PublishError("Publication service returned an invalid response")
    return body


async def _credentials(client: httpx.AsyncClient, context: PublishContext, repo: str) -> dict:
    try:
        response = await client.get(
            context.backend_url.rstrip("/") + "/auth/github/publish-token",
            headers={"Authorization": f"Bearer {context.auth_token}"},
            params={"guild_id": context.guild_id, "task_id": context.task_id, "repo": repo},
        )
    except httpx.HTTPError as exc:
        raise SigningUnavailable("Cannot reach App publication service") from exc
    if response.status_code == 503:
        raise SigningUnavailable("GitHub App signing is not configured or is unavailable")
    credentials = _response_json(response)
    if not credentials.get("access_token") or not str(credentials.get("username", "")).endswith(
        "[bot]"
    ):
        raise PublishError("Publication service did not return GitHub App credentials")
    return credentials


async def check_access(worktree: str | Path, context: PublishContext) -> None:
    """Check App credentials, distinguishing optional signing from authorization errors."""
    async with httpx.AsyncClient(timeout=60) as client:
        await _credentials(client, context, await _repository(Path(worktree)))


async def publish_checkpoint(
    worktree: str | Path,
    context: PublishContext,
    message: str,
    *,
    fallback_token: str | None = None,
) -> str:
    """Stage all files; publish one snapshot, preserving later edits and reflog history.

    Existing unpublished local commits are combined into this checkpoint. Neither
    the worktree nor index is reset: moving HEAD alone completes the local commit.
    """
    cwd = Path(worktree)
    cwd = Path((await _git(["rev-parse", "--show-toplevel"], cwd=cwd)).decode().strip())
    git_dir = Path((await _git(["rev-parse", "--absolute-git-dir"], cwd=cwd)).decode().strip())
    with (git_dir / "pioneer-publish.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PublishError("Another publication is running in this worktree") from exc
        try:
            return await _publish(cwd, context, message)
        except SigningUnavailable as exc:
            logger.warning("%s; publishing an unsigned Git checkpoint", exc)
            return await _publish_unsigned(cwd, context, message, fallback_token)
        except httpx.HTTPError as exc:
            raise PublishError(
                "Publication request interrupted; rerun pioneer publish to recover. Local work is preserved."
            ) from exc


async def _publish_unsigned(
    cwd: Path, context: PublishContext, message: str, token: str | None
) -> str:
    """Use existing Git transport credentials when optional App signing is unavailable."""
    branch = (await _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)).decode().strip()
    if branch not in ("HEAD", context.branch):
        raise PublishError("Worktree changed branches; refusing unsigned publication")
    default_ref = (
        (await _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)).decode().strip()
    )
    if default_ref == f"refs/remotes/origin/{context.branch}":
        raise PublishError("Task publication cannot target the repository's default branch")
    if await _git(["status", "--porcelain"], cwd=cwd):
        await _git(["add", "-A"], cwd=cwd)
        name = (
            (await _git(["config", "--get", "--default", "Pioneer Square", "user.name"], cwd=cwd))
            .decode()
            .strip()
        )
        email = (
            (
                await _git(
                    ["config", "--get", "--default", "pioneer-square@localhost", "user.email"],
                    cwd=cwd,
                )
            )
            .decode()
            .strip()
        )
        await _git(
            [
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-m",
                message,
            ],
            cwd=cwd,
            extra_env=commit_env(context),
        )
    try:
        base = (
            (
                await _git(
                    ["rev-parse", "--verify", f"refs/remotes/origin/{context.branch}"], cwd=cwd
                )
            )
            .decode()
            .strip()
        )
    except PublishError:
        base = "origin/HEAD"
    ahead = (await _git(["rev-list", "--count", f"{base}..HEAD"], cwd=cwd)).decode().strip()
    if ahead == "0":
        return "nothing"
    repo = await _repository(cwd)
    await _git(
        ["push", f"https://github.com/{repo}.git", f"HEAD:refs/heads/{context.branch}"],
        cwd=cwd,
        token=token,
    )
    await _git(["update-ref", f"refs/remotes/origin/{context.branch}", "HEAD"], cwd=cwd)
    return "pushed"


async def _publish(cwd: Path, context: PublishContext, message: str) -> str:
    if not message.strip():
        raise PublishError("A checkpoint message is required")
    await _git(["check-ref-format", "--branch", context.branch], cwd=cwd)
    branch = (await _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)).decode().strip()
    if branch not in ("HEAD", context.branch):
        raise PublishError("Worktree branch does not match this task's publication branch")
    source = (await _git(["rev-parse", "HEAD"], cwd=cwd)).decode().strip()
    repo = await _repository(cwd)
    async with httpx.AsyncClient(timeout=60) as client:
        credentials = await _credentials(client, context, repo)
        token = credentials["access_token"]
        logger.info(
            "Publishing %s@%s as %s: author=%s (committer chosen by GitHub; expires %s)",
            repo,
            context.branch,
            credentials["username"],
            context.author,
            credentials.get("expires_at"),
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async def api(method, path, **kwargs):
            return await _json(
                client, method, "https://api.github.com" + path, headers=headers, **kwargs
            )

        metadata = await api("GET", f"/repos/{repo}")
        if context.branch == metadata.get("default_branch"):
            raise PublishError("Task publication cannot target the repository's default branch")
        ref_path = f"/repos/{repo}/git/ref/heads/{quote(context.branch, safe='/')}"
        ref = await client.get("https://api.github.com" + ref_path, headers=headers)
        if ref.status_code not in (200, 404):
            raise PublishError(f"Cannot read task branch (HTTP {ref.status_code})")
        remote = ref.json()["object"]["sha"] if ref.status_code == 200 else None
        if remote:
            await _git(
                ["fetch", "--no-tags", f"https://github.com/{repo}.git", remote],
                cwd=cwd,
                token=token,
            )
            parent = remote
        else:
            parent = (await _git(["merge-base", source, "origin/HEAD"], cwd=cwd)).decode().strip()
        # Like git add -A && git commit, this intentionally includes staged and unstaged work.
        await _git(["add", "-A"], cwd=cwd)
        tree = (await _git(["write-tree"], cwd=cwd)).decode().strip()
        parent_tree = (await _git(["rev-parse", parent + "^{tree}"], cwd=cwd)).decode().strip()
        marker = f"Pioneer-Task: {context.task_id}\nPioneer-Source: {source}\nPioneer-Tree: {tree}"

        async def verified_commit(sha):
            commit = await api("GET", f"/repos/{repo}/commits/{sha}")
            verification = commit["commit"]["verification"]
            author = (commit.get("author") or {}).get("login")
            committer = (commit.get("committer") or {}).get("login")
            # Signing may degrade to unsigned Git, but identity/content must still match.
            if committer not in (credentials["username"], WEB_FLOW):
                raise PublishError("Commit committer does not match the configured GitHub App")
            if not context.author and author != credentials["username"]:
                raise PublishError("Commit author does not match the configured GitHub App")
            if commit["commit"]["tree"]["sha"] != tree:
                raise PublishError("App commit does not contain the expected snapshot")
            if not verification["verified"] or verification["reason"] != "valid":
                logger.warning(
                    "GitHub did not verify %s: reason=%s author=%s committer=%s "
                    "requested_author=%s app=%s",
                    sha,
                    verification.get("reason"),
                    author,
                    committer,
                    context.author,
                    credentials["username"],
                )
                raise SigningUnavailable("GitHub did not verify the App commit")
            return commit

        async def sync_head(sha):
            await _git(
                ["fetch", "--no-tags", f"https://github.com/{repo}.git", sha], cwd=cwd, token=token
            )
            current_branch = (
                (await _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)).decode().strip()
            )
            if current_branch != branch:
                raise PublishError(
                    "Worktree changed branches; remote checkpoint is safe, local work is preserved"
                )
            target = "HEAD" if branch == "HEAD" else f"refs/heads/{branch}"
            await _git(
                [
                    "update-ref",
                    "--no-deref",
                    "-m",
                    "pioneer publish checkpoint",
                    target,
                    sha,
                    source,
                ],
                cwd=cwd,
            )

        # A ref update may have landed even though its HTTP response was lost.
        if remote and tree == parent_tree:
            commit = await api("GET", f"/repos/{repo}/commits/{remote}")
            if commit["commit"]["message"].endswith(marker):
                await verified_commit(remote)
                await sync_head(remote)
                return "pushed"
        common = (await _git(["merge-base", source, parent], cwd=cwd)).decode().strip()
        if common != parent:
            raise PublishError("Remote task branch diverged; reconcile it before publishing")
        if tree == parent_tree:
            return "nothing"

        raw = await _git(
            [
                "diff-tree",
                "-r",
                "--raw",
                "-z",
                "--no-renames",
                "--no-commit-id",
                "--abbrev=40",
                parent,
                tree,
            ],
            cwd=cwd,
        )
        records = raw.rstrip(b"\0").split(b"\0")
        entries = []
        for header, path_bytes in zip(records[::2], records[1::2], strict=True):
            old_mode, new_mode, old_oid, new_oid, _status = header.decode().split()
            old_mode = old_mode.removeprefix(":")
            try:
                path = path_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PublishError("GitHub publication requires UTF-8 file names") from exc
            mode = old_mode if new_mode == "000000" else new_mode
            oid = new_oid if new_mode != "000000" else None
            if oid and mode != "160000" and new_oid != old_oid:
                contents = await _git(["cat-file", "blob", oid], cwd=cwd)
                if contents.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                    raise PublishError(
                        "Git LFS object uploads are not supported by pioneer publish"
                    )
                blob = await api(
                    "POST",
                    f"/repos/{repo}/git/blobs",
                    json={"encoding": "base64", "content": base64.b64encode(contents).decode()},
                )
                if blob["sha"] != oid:
                    raise PublishError("Uploaded blob does not match the local Git object")
            entries.append(
                {
                    "path": path,
                    "mode": mode,
                    "type": "commit" if mode == "160000" else "blob",
                    "sha": oid,
                }
            )
        made_tree = await api(
            "POST", f"/repos/{repo}/git/trees", json={"base_tree": parent_tree, "tree": entries}
        )
        if made_tree["sha"] != tree:
            raise PublishError("GitHub tree differs from the local snapshot; refusing publication")
        made = await api(
            "POST",
            f"/repos/{repo}/git/commits",
            json={
                "tree": tree,
                "parents": [parent],
                "message": message.rstrip() + "\n\n" + marker,
                # Triggering user authors. Never send a committer: GitHub only signs
                # when it picks the committer itself (web-flow); explicit ones are unsigned.
                **({"author": _api_identity(context.author)} if context.author else {}),
            },
        )
        logger.info(
            "Created commit %s: verification=%s",
            made.get("sha"),
            made.get("verification"),
        )
        await verified_commit(made["sha"])
        # updateRefs adds exact compare-and-swap semantics missing from REST ref updates.
        update = await api(
            "POST",
            "/graphql",
            json={
                "query": "mutation($input: UpdateRefsInput!) { updateRefs(input: $input) { clientMutationId } }",
                "variables": {
                    "input": {
                        "repositoryId": metadata["node_id"],
                        "refUpdates": [
                            {
                                "name": "refs/heads/" + context.branch,
                                "beforeOid": remote or "0" * 40,
                                "afterOid": made["sha"],
                                "force": False,
                            }
                        ],
                    }
                },
            },
        )
        if update.get("errors") or not update.get("data", {}).get("updateRefs"):
            raise PublishError(
                "Remote branch moved or rejected publication; local work is preserved"
            )
        await sync_head(made["sha"])
        return "pushed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pioneer publish",
        description="Publish a checkpoint, App-signed when configured; otherwise warn and use unsigned Git.",
    )
    parser.add_argument("-m", "--message", required=True)
    args = parser.parse_args(argv)
    # PIONEER_PUBLISH_LOG_LEVEL=INFO traces identity, token expiry, and GitHub responses.
    logging.basicConfig(level=os.environ.get("PIONEER_PUBLISH_LOG_LEVEL", "WARNING").upper())
    try:
        result = asyncio.run(
            publish_checkpoint(
                Path.cwd(),
                PublishContext.from_env(),
                args.message,
                fallback_token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"),
            )
        )
    except (PublishError, OSError) as exc:
        print(f"pioneer publish: {exc}", file=sys.stderr)
        return 1
    print("Checkpoint published." if result == "pushed" else "No changes to publish.")
    return 0
