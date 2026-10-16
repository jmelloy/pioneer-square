"""gh invocations must use the task's token, not the ambient GITHUB_TOKEN."""

import asyncio
from unittest.mock import patch

from pioneer_worker import git_ops


class _Proc:
    returncode = 0

    async def communicate(self):
        return b"", b""


def _capture(monkeypatch_env, token):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["env"] = kwargs.get("env")
        return _Proc()

    with patch.object(asyncio, "create_subprocess_exec", fake_exec):
        asyncio.run(git_ops.run_gh(["pr", "view"], token=token))
    return seen["env"]


def test_gh_token_env():
    assert git_ops.gh_token_env("t") == {"GH_TOKEN": "t", "GITHUB_TOKEN": "t"}
    assert git_ops.gh_token_env(None) == {}


def test_run_gh_overrides_ambient_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ambient")
    monkeypatch.setenv("GH_TOKEN", "ambient")
    env = _capture(monkeypatch, "task-tok")
    assert env["GH_TOKEN"] == "task-tok"
    assert env["GITHUB_TOKEN"] == "task-tok"


def test_run_gh_without_token_inherits(monkeypatch):
    assert _capture(monkeypatch, None) is None
