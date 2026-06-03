"""Unit tests for pioneer_worker.pi_runner."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from pioneer_worker.pi_runner import parse_pi_event, run_pi_auto

# ---------------------------------------------------------------------------
# parse_pi_event
# ---------------------------------------------------------------------------


def test_parse_agent_start():
    text, last = parse_pi_event({"type": "agent_start"}, "")
    assert text == "[pi] agent started"
    assert last == ""


def test_parse_message_update_full_text():
    event = {
        "type": "message_update",
        "message": {"content": [{"type": "text", "text": "Hello world"}]},
    }
    text, last = parse_pi_event(event, "")
    assert text == "Hello world"
    assert last == "Hello world"


def test_parse_message_update_delta_only():
    event = {
        "type": "message_update",
        "message": {"content": [{"type": "text", "text": "Hello world extended"}]},
    }
    text, last = parse_pi_event(event, "Hello world")
    # delta = " extended" (stripped) which has content
    assert text is not None
    assert "extended" in text
    assert last == "Hello world extended"


def test_parse_message_update_whitespace_delta_returns_none():
    event = {
        "type": "message_update",
        "message": {"content": [{"type": "text", "text": "Hello world  "}]},
    }
    # delta is only whitespace → None
    text, last = parse_pi_event(event, "Hello world  ")
    assert text is None


def test_parse_tool_execution_start_bash():
    event = {
        "type": "tool_execution_start",
        "toolName": "bash",
        "args": {"command": "ls -la"},
    }
    text, _ = parse_pi_event(event, "")
    assert text == "▶ bash: ls -la"


def test_parse_tool_execution_start_read():
    event = {
        "type": "tool_execution_start",
        "toolName": "read",
        "args": {"path": "/tmp/foo.py"},
    }
    text, _ = parse_pi_event(event, "")
    assert text == "▶ read: /tmp/foo.py"


def test_parse_tool_execution_start_edit():
    event = {
        "type": "tool_execution_start",
        "toolName": "edit",
        "args": {"file_path": "/tmp/bar.py"},
    }
    text, _ = parse_pi_event(event, "")
    assert text == "▶ edit: /tmp/bar.py"


def test_parse_tool_execution_start_unknown():
    event = {
        "type": "tool_execution_start",
        "toolName": "custom_tool",
        "args": {"key": "val"},
    }
    text, _ = parse_pi_event(event, "")
    assert text is not None
    assert "custom_tool" in text


def test_parse_tool_execution_end_success():
    event = {
        "type": "tool_execution_end",
        "result": {"content": [{"type": "text", "text": "output text"}]},
        "isError": False,
    }
    text, _ = parse_pi_event(event, "")
    assert text == "  → output text"


def test_parse_tool_execution_end_error():
    event = {
        "type": "tool_execution_end",
        "result": {"content": [{"type": "text", "text": "command failed"}]},
        "isError": True,
    }
    text, _ = parse_pi_event(event, "")
    assert text == "  ✗ command failed"


def test_parse_tool_execution_end_empty_returns_none():
    event = {
        "type": "tool_execution_end",
        "result": {"content": []},
        "isError": False,
    }
    text, _ = parse_pi_event(event, "")
    assert text is None


def test_parse_tool_execution_end_multiline_shows_count():
    event = {
        "type": "tool_execution_end",
        "result": {"content": [{"type": "text", "text": "line1\nline2\nline3"}]},
        "isError": False,
    }
    text, _ = parse_pi_event(event, "")
    assert text is not None
    assert "+2 lines" in text


def test_parse_unknown_event_returns_none():
    text, last = parse_pi_event({"type": "unknown_type"}, "existing")
    assert text is None
    assert last == "existing"


def test_parse_empty_event_returns_none():
    text, last = parse_pi_event({}, "abc")
    assert text is None
    assert last == "abc"


# ---------------------------------------------------------------------------
# run_pi_auto — integration with fake pi scripts
# ---------------------------------------------------------------------------


async def test_run_pi_auto_success(tmp_path) -> None:
    """Happy path: pi emits agent_start → message_update → agent_end and exits."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_start"}}), flush=True)
print(json.dumps({{"type": "message_update", "message": {{"content": [{{"type": "text", "text": "Task done"}}]}}}}), flush=True)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    success, stop_reason, last_text = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is True
    assert stop_reason == "success"
    assert "[pi] agent started" in emitted
    assert last_text == "Task done"
    for line in emitted:
        assert not line.endswith("\n"), f"trailing newline in emitted line: {line!r}"


async def test_run_pi_auto_not_found(tmp_path) -> None:
    """Missing pi binary → FileNotFoundError → returns (False, 'no_events', '')."""
    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    success, stop_reason, last_text = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path="/nonexistent/pi-binary-xyz"
    )

    assert success is False
    assert stop_reason == "no_events"
    assert any("not found" in line for line in emitted)


async def test_run_pi_auto_prompt_rejected(tmp_path) -> None:
    """Pi sends a failed response event → saw_error=True, stop_reason='error_during_execution'."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "response", "command": "prompt", "success": False, "error": "auth failure"}}), flush=True)
sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    success, stop_reason, last_text = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is False
    assert stop_reason == "error_during_execution"
    assert any("auth failure" in line for line in emitted)


async def test_run_pi_auto_non_json_stdout(tmp_path) -> None:
    """Non-JSON stdout lines are emitted verbatim and don't crash the runner."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print("plain text line", flush=True)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    success, stop_reason, _ = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert "plain text line" in emitted
    assert stop_reason == "success"


async def test_run_pi_auto_message_update_error(tmp_path) -> None:
    """assistantMessageEvent error in message_update sets saw_error."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
event = {{
    "type": "message_update",
    "assistantMessageEvent": {{"type": "error", "reason": "context_limit"}},
    "message": {{"content": []}}
}}
print(json.dumps(event), flush=True)
sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    success, stop_reason, _ = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is False
    assert stop_reason == "error_during_execution"
    assert any("context_limit" in line for line in emitted)


async def test_run_pi_auto_hung_process_is_killed(tmp_path, monkeypatch) -> None:
    """Pi doesn't exit after agent_end + stdin close → timeout fires, SIGKILL sent."""
    import pioneer_worker.pi_runner as pi_runner_mod

    monkeypatch.setattr(pi_runner_mod, "_WAIT_TIMEOUT", 0.1)

    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys, signal, time

print(json.dumps({{"type": "agent_end"}}), flush=True)

# Ignore SIGTERM so only SIGKILL (proc.kill()) will stop us.
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    # Should complete (not hang) even though pi refuses to exit on its own.
    success, stop_reason, _ = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    # agent_end was received, so stop_reason should be success regardless of
    # how the process was ultimately reaped.
    assert stop_reason == "success"


async def test_run_pi_auto_large_line_does_not_crash(tmp_path) -> None:
    """A stdout line larger than the default 64 KB limit must not crash the runner."""
    fake_pi = tmp_path / "fake-pi"
    # Write a line that is 2 MB long (well above asyncio's default 64 KB limit but
    # within the 10 MB _STREAM_LIMIT, so it succeeds cleanly).
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print("x" * 2 * 1024 * 1024, flush=True)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    # Should complete without raising — either success or graceful error.
    success, stop_reason, _ = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    # The 2 MB line is below the 10 MB _STREAM_LIMIT, so the runner completes cleanly.
    assert stop_reason == "success"


async def test_run_pi_auto_limit_overrun_skips_and_continues() -> None:
    """LimitOverrunError on the first readline is logged and skipped; the
    following agent_end line is still processed and stop_reason is success.
    Uses a fully-mocked subprocess so no real process is spawned."""
    from unittest.mock import AsyncMock, MagicMock, patch

    # --- stdout behaviour ---
    # readline: first call raises LimitOverrunError; second returns agent_end; third is EOF.
    readline_queue = [
        asyncio.LimitOverrunError("line too long", 0),
        b'{"type":"agent_end"}\n',
        b"",
    ]
    readline_pos = [0]

    async def _readline() -> bytes:
        idx = readline_pos[0]
        readline_pos[0] += 1
        val = readline_queue[idx] if idx < len(readline_queue) else b""
        if isinstance(val, BaseException):
            raise val
        return val

    # read(1): drain loop reads two bytes — 'x' then '\n' — to simulate the
    # tail of the oversized line, stopping exactly at the newline.
    read_queue = [b"x", b"\n"]
    read_pos = [0]

    async def _read(n: int) -> bytes:
        idx = read_pos[0]
        read_pos[0] += 1
        return read_queue[idx] if idx < len(read_queue) else b""

    fake_stdout = MagicMock()
    fake_stdout.readline = _readline
    fake_stdout.read = _read

    # --- stderr: empty async generator ---
    async def _empty_stderr():
        return
        yield  # make it an async generator

    # --- stdin ---
    fake_stdin = MagicMock()
    fake_stdin.write = MagicMock()
    fake_stdin.drain = AsyncMock()
    fake_stdin.close = MagicMock()
    fake_stdin.is_closing = MagicMock(return_value=False)

    # --- process ---
    fake_proc = MagicMock()
    fake_proc.pid = 99999
    fake_proc.returncode = 0  # already exited — finally block skips kill
    fake_proc.stdout = fake_stdout
    fake_proc.stderr = _empty_stderr()
    fake_proc.stdin = fake_stdin
    fake_proc.wait = AsyncMock(return_value=0)

    async def _fake_create(*args: object, **kwargs: object):
        return fake_proc

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    with patch("asyncio.create_subprocess_exec", new=_fake_create):
        success, stop_reason, _ = await run_pi_auto("/tmp", "/tmp", emit=emit)

    # The oversized-line error must have been reported.
    assert any("too large" in line for line in emitted)
    # agent_end was processed → success is True (not merely exit_code==0).
    assert success is True
    assert stop_reason == "success"
    # No emitted line should carry a spurious trailing newline.
    for line in emitted:
        assert not line.endswith("\n"), f"trailing newline in emitted line: {line!r}"


async def test_run_pi_auto_stderr_forwarded(tmp_path) -> None:
    """Stderr lines from pi are forwarded to emit with [stderr] prefix."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print("warning from pi", file=sys.stderr, flush=True)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str) -> None:
        emitted.append(line)

    await run_pi_auto("do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi))

    assert any("[stderr]" in line and "warning from pi" in line for line in emitted)
