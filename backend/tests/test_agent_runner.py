"""Unit tests for agent_runner's Pi RPC handling.

Regression coverage for a bug where the one-off "Run" button's Pi invocation
sent {"type": "prompt", "content": ...} instead of the {"message": ...} field
pi's RPC handler actually reads. Confirmed against the real `pi` binary:
"content" makes pi crash internally and reply with
{"type": "response", "command": "prompt", "success": false,
 "error": "Cannot read properties of undefined (reading 'startsWith')"},
which stream_agent silently dropped (no case in parse_event) and then hung
forever, since pi's RPC session never closes stdout on its own.

All external calls (subprocess, DB, broadcast) are mocked — no real
network/process usage.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import agent_runner  # noqa: E402
from agent_runner import RunAgentRequest, build_command, stream_agent  # noqa: E402


def test_build_command_pi_uses_rpc_mode():
    cmd, needs_stdin = build_command(RunAgentRequest(tool="pi", prompt="do a thing"))
    assert cmd[:3] == ["pi", "--mode", "rpc"]
    assert needs_stdin is True


class _FakeStdout:
    """Async-iterable feeding pre-baked JSONL events, then EOF forever."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode() + b"\n" for line in lines]
        self._i = 0

    async def readline(self) -> bytes:
        if self._i >= len(self._lines):
            return b""
        line = self._lines[self._i]
        self._i += 1
        return line

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class _FakeStderr:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeProc:
    def __init__(self, stdout_lines: list[str]) -> None:
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdin.close = MagicMock()
        self.stdin.is_closing = MagicMock(return_value=False)
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr()
        self.returncode = 0

    async def wait(self) -> int:
        return 0


async def _run_stream_agent(stdout_lines: list[str]) -> tuple[_FakeProc, list[str]]:
    proc = _FakeProc(stdout_lines)
    emitted: list[str] = []

    async def fake_emit_terminal_line(guild_id, agent_id, line):
        emitted.append(line)

    with (
        patch.object(agent_runner, "set_agent_state", AsyncMock()),
        patch.object(agent_runner, "emit_terminal_line", fake_emit_terminal_line),
        patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc),
        ),
    ):
        req = RunAgentRequest(tool="pi", prompt="say hello")
        await asyncio.wait_for(stream_agent("g1", "a1", req), timeout=5)
    return proc, emitted


@pytest.mark.asyncio
async def test_stream_agent_sends_message_field_not_content():
    """The RPC prompt frame must use the "message" key pi's handler reads."""
    proc, _ = await _run_stream_agent(['{"type": "agent_end"}'])
    sent = proc.stdin.write.call_args[0][0].decode()
    payload = json.loads(sent)
    assert payload == {"type": "prompt", "message": "say hello"}
    assert "content" not in payload


@pytest.mark.asyncio
async def test_stream_agent_surfaces_rejected_prompt_and_does_not_hang():
    """A pi RPC rejection (success=false) must be surfaced and end the stream.

    Before the fix, parse_event() had no case for "response" events, so a
    rejected prompt was silently dropped and the read loop waited forever for
    another stdout line pi's still-alive RPC session was never going to send.
    """
    rejection = json.dumps(
        {
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "Cannot read properties of undefined (reading 'startsWith')",
        }
    )
    proc, emitted = await _run_stream_agent([rejection])
    assert any("startsWith" in line for line in emitted)


@pytest.mark.asyncio
async def test_stream_agent_stops_reading_after_agent_end():
    """agent_end must end the read loop instead of hanging on pi's open RPC session."""
    proc, emitted = await _run_stream_agent(['{"type": "agent_start"}', '{"type": "agent_end"}'])
    assert proc.stdin.close.called
