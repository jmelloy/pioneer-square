"""Tests for ClaudeRunner.is_authenticated.

Covers the JSON-parsing path used to detect Claude credentials, plus the
timeout guard that prevents macOS keychain prompts from hanging worker
startup forever. The probe lives on the runner rather than the worker: it is
claude's own credential logic, so it belongs behind the Runner seam (#1238).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from pioneer_worker.claude_runner import ClaudeRunner


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes, *, hang: bool = False) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._hang = hang
        self.pid = 12345
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return (self._stdout, b"")

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


async def test_authenticated_when_logged_in_true():
    runner = ClaudeRunner()
    proc = _FakeProc(0, b'{"loggedIn": true, "authMethod": "claude_login"}')
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await runner.is_authenticated() is True


async def test_not_authenticated_when_logged_in_false():
    """Regression: rc=0 with loggedIn:false must NOT be treated as authed."""
    runner = ClaudeRunner()
    proc = _FakeProc(0, b'{"loggedIn": false}')
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await runner.is_authenticated() is False


async def test_not_authenticated_when_nonzero_rc():
    runner = ClaudeRunner()
    proc = _FakeProc(2, b"unknown command")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await runner.is_authenticated() is False


async def test_legacy_non_json_output_falls_back_to_rc():
    """Older claude versions returned plain text; rc=0 should still mean authed."""
    runner = ClaudeRunner()
    proc = _FakeProc(0, b"You are logged in.")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await runner.is_authenticated() is True


async def test_timeout_returns_false_and_kills_process():
    """If `claude auth status` hangs (e.g. macOS keychain prompt), don't block startup."""
    runner = ClaudeRunner()
    proc = _FakeProc(0, b"", hang=True)

    async def fake_wait_for(coro, timeout):
        coro.close()  # avoid "coroutine was never awaited" warning
        raise TimeoutError()

    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch("asyncio.wait_for", fake_wait_for),
    ):
        assert await runner.is_authenticated() is False
    assert proc.killed is True


async def test_missing_claude_binary_returns_false():
    runner = ClaudeRunner()
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError())):
        assert await runner.is_authenticated() is False
