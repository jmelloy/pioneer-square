"""Credentials must authenticate a single git call, never persist in a clone.

A token baked into ``remote.origin.url`` is reused by every later fetch on that
shared clone regardless of which user the fetch is for, so the first caller's
token silently serves everyone else's tasks and keeps being used after it stops
being valid. These tests pin the invariant and the migration path for clones
made before it held.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pioneer_worker import git_ops  # noqa: E402


def _git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_origin(tmp_path: Path) -> str:
    """A real bare repo with one commit, usable as an origin for clones."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        _git("config", k, v, cwd=str(src))
    (src / "README.md").write_text("hi\n")
    _git("add", "-A", cwd=str(src))
    _git("commit", "-qm", "init", cwd=str(src))
    _git("push", "-q", str(origin), "main", cwd=str(src))
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=str(origin))
    return str(origin)


async def test_auth_env_carries_token_out_of_argv_and_config():
    env = git_ops._auth_env("fake_token_for_tests")
    assert env is not None
    # The token rides in a git config value delivered by environment, so it is
    # not in the command line and not written to any repo's config file.
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"].startswith("http.https://github.com/")
    expected = base64.b64encode(b"x-access-token:fake_token_for_tests").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"
    assert git_ops._auth_env(None) is None
    assert git_ops._auth_env("") is None


async def test_run_git_passes_token_only_to_that_invocation(tmp_path):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = args
        captured["env"] = kwargs.get("env")

        class _P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        return _P()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await git_ops.run_git(["fetch", "origin"], cwd=str(tmp_path), token="fake_token_for_tests")
    assert "fake_token_for_tests" not in " ".join(str(a) for a in captured["argv"])
    assert captured["env"]["GIT_CONFIG_COUNT"] == "1"

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await git_ops.run_git(["status"], cwd=str(tmp_path))
    # No token — no config injection, so the process just inherits the parent env.
    assert captured["env"] is None


async def test_clone_does_not_persist_the_token_in_git_config(tmp_path):
    origin = _make_origin(tmp_path)
    repos_dir = tmp_path / "repos"

    # ensure_repo builds a github.com URL, so point the clone at the local origin
    # by intercepting the URL while leaving the token handling under test.
    real_run_git = git_ops.run_git

    async def run_git(args, cwd=None, token=None):
        args = [origin if a.startswith("https://github.com/") else a for a in args]
        return await real_run_git(args, cwd=cwd, token=token)

    with patch.object(git_ops, "run_git", side_effect=run_git):
        path = await git_ops.ensure_repo(str(repos_dir), "owner/repo", token="fake_token_for_tests")

    assert path is not None
    config = (Path(path) / ".git" / "config").read_text()
    assert "fake_token_for_tests" not in config
    assert _git("config", "--get", "remote.origin.url", cwd=path) == origin


async def test_existing_clone_with_an_embedded_token_is_scrubbed(tmp_path):
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", origin, str(clone)], check=True)
    # Simulate a clone made by the old credentialed-URL code path.
    _git(
        "remote",
        "set-url",
        "origin",
        "https://ghp_old_token@github.com/owner/repo.git",
        cwd=str(clone),
    )

    assert await git_ops.scrub_remote_credentials(str(clone)) is True

    url = _git("config", "--get", "remote.origin.url", cwd=str(clone))
    assert url == "https://github.com/owner/repo.git"
    assert "ghp_old_token" not in (clone / ".git" / "config").read_text()
    # Idempotent: a clean URL is left exactly as it is.
    assert await git_ops.scrub_remote_credentials(str(clone)) is False
    assert _git("config", "--get", "remote.origin.url", cwd=str(clone)) == url


async def test_scrub_leaves_ssh_and_local_remotes_alone(tmp_path):
    origin = _make_origin(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", origin, str(clone)], check=True)
    _git("remote", "set-url", "origin", "git@github.com:owner/repo.git", cwd=str(clone))
    assert await git_ops.scrub_remote_credentials(str(clone)) is False
    assert _git("config", "--get", "remote.origin.url", cwd=str(clone)) == (
        "git@github.com:owner/repo.git"
    )
