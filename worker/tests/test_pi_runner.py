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
    text, _, last = parse_pi_event({"type": "agent_start"}, "")
    assert text == "[pi] agent started"
    assert last == ""


def test_parse_message_update_full_text():
    event = {
        "type": "message_update",
        "message": {"content": [{"type": "text", "text": "Hello world"}]},
    }
    text, _, last = parse_pi_event(event, "")
    assert text == "Hello world"
    assert last == "Hello world"


def test_parse_message_update_delta_only():
    event = {
        "type": "message_update",
        "message": {"content": [{"type": "text", "text": "Hello world extended"}]},
    }
    text, _, last = parse_pi_event(event, "Hello world")
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
    text, _, last = parse_pi_event(event, "Hello world  ")
    assert text is None


def test_parse_tool_execution_start_bash():
    event = {
        "type": "tool_execution_start",
        "toolName": "bash",
        "args": {"command": "ls -la"},
    }
    text, _, _ = parse_pi_event(event, "")
    assert text == "▶ bash: ls -la"


def test_parse_tool_execution_start_read():
    event = {
        "type": "tool_execution_start",
        "toolName": "read",
        "args": {"path": "/tmp/foo.py"},
    }
    text, _, _ = parse_pi_event(event, "")
    assert text == "▶ read: /tmp/foo.py"


def test_parse_tool_execution_start_edit():
    event = {
        "type": "tool_execution_start",
        "toolName": "edit",
        "args": {"file_path": "/tmp/bar.py"},
    }
    text, _, _ = parse_pi_event(event, "")
    assert text == "▶ edit: /tmp/bar.py"


def test_parse_tool_execution_start_read_strips_worktree_prefix():
    event = {
        "type": "tool_execution_start",
        "toolName": "read",
        "args": {"path": "/work/dnsid/w-4de676/t-vdfpj3/dnsid-infra/envs/dev/main.tf"},
    }
    text, _, _ = parse_pi_event(event, "")
    assert text == "▶ read: dnsid-infra/envs/dev/main.tf"


def test_parse_tool_execution_start_unknown():
    event = {
        "type": "tool_execution_start",
        "toolName": "custom_tool",
        "args": {"key": "val"},
    }
    text, _, _ = parse_pi_event(event, "")
    assert text is not None
    assert "custom_tool" in text


def test_parse_tool_execution_end_success():
    event = {
        "type": "tool_execution_end",
        "result": {"content": [{"type": "text", "text": "output text"}]},
        "isError": False,
    }
    text, _, _ = parse_pi_event(event, "")
    assert text == "  → output text"


def test_parse_tool_execution_end_error():
    event = {
        "type": "tool_execution_end",
        "result": {"content": [{"type": "text", "text": "command failed"}]},
        "isError": True,
    }
    text, _, _ = parse_pi_event(event, "")
    assert text == "  ✗ command failed"


def test_parse_tool_execution_end_empty_returns_none():
    event = {
        "type": "tool_execution_end",
        "result": {"content": []},
        "isError": False,
    }
    text, _, _ = parse_pi_event(event, "")
    assert text is None


def test_parse_tool_execution_end_multiline_shows_count():
    event = {
        "type": "tool_execution_end",
        "result": {"content": [{"type": "text", "text": "line1\nline2\nline3"}]},
        "isError": False,
    }
    text, _, _ = parse_pi_event(event, "")
    assert text is not None
    assert "+2 lines" in text


def test_parse_tool_execution_end_preserves_full_output_in_detail():
    """The display line is truncated to the first line, but detail carries the full output (#781)."""
    long_output = "\n".join(f"line{i}" for i in range(100))
    event = {
        "type": "tool_execution_end",
        "result": {"content": [{"type": "text", "text": long_output}]},
        "isError": False,
    }
    text, detail, _ = parse_pi_event(event, "")
    assert text == "  → line0 (+99 lines)"
    assert detail == {"toolType": "tool_result", "output": long_output}


def test_parse_tool_execution_start_preserves_full_input_in_detail():
    event = {
        "type": "tool_execution_start",
        "toolName": "bash",
        "args": {"command": "ls -la"},
    }
    _, detail, _ = parse_pi_event(event, "")
    assert detail == {"toolType": "tool_use", "name": "bash", "input": {"command": "ls -la"}}


def test_parse_unknown_event_returns_none():
    text, _, last = parse_pi_event({"type": "unknown_type"}, "existing")
    assert text is None
    assert last == "existing"


def test_parse_empty_event_returns_none():
    text, _, last = parse_pi_event({}, "abc")
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is True
    assert stop_reason == "success"
    assert "[pi] agent started" in emitted
    assert last_text == "Task done"
    assert session_id is None  # no session event emitted by this fake
    for line in emitted:
        assert not line.endswith("\n"), f"trailing newline in emitted line: {line!r}"


async def test_run_pi_auto_publishes_proc_handle(tmp_path) -> None:
    """on_proc receives a live, terminate-capable handle for the pi subprocess."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    handles: list[object] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        pass

    await run_pi_auto(
        "do the work",
        str(tmp_path),
        emit=emit,
        pi_path=os.fspath(fake_pi),
        on_proc=handles.append,
    )

    assert len(handles) == 1
    handle = handles[0]
    assert hasattr(handle, "terminate")
    assert hasattr(handle, "send_message")
    # terminate() on an already-exited proc is a no-op, not a crash.
    await handle.terminate()


async def test_run_pi_auto_not_found(tmp_path) -> None:
    """Missing pi binary → FileNotFoundError → returns (False, 'no_events', '', None)."""
    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path="/nonexistent/pi-binary-xyz"
    )

    assert success is False
    assert stop_reason == "no_events"
    assert session_id is None
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, _, _sid = await run_pi_auto(
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, _, _sid = await run_pi_auto(
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    # Should complete (not hang) even though pi refuses to exit on its own.
    success, stop_reason, _, _sid = await run_pi_auto(
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    # Should complete without raising — either success or graceful error.
    success, stop_reason, _, _sid = await run_pi_auto(
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    with patch("asyncio.create_subprocess_exec", new=_fake_create):
        success, stop_reason, _, _sid = await run_pi_auto("/tmp", "/tmp", emit=emit)

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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    await run_pi_auto("do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi))

    assert any("[stderr]" in line and "warning from pi" in line for line in emitted)


# ---------------------------------------------------------------------------
# Goal 1: Error & Crash Reporting
# ---------------------------------------------------------------------------


async def test_run_pi_auto_nonzero_exit_emits_error(tmp_path) -> None:
    """Pi exits non-zero without agent_end → error chunk emitted, success=False."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_start"}}), flush=True)
print(json.dumps({{"type": "message_update", "message": {{"content": [{{"type": "text", "text": "partial"}}]}}}}), flush=True)
sys.exit(2)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is False
    assert stop_reason == "error_during_execution"
    # Partial text should be preserved
    assert last_text == "partial"
    # An explicit error message about the exit code must be emitted
    assert any("non-zero" in line and "2" in line for line in emitted), emitted


async def test_run_pi_auto_crash_with_stderr(tmp_path) -> None:
    """Pi crashes (non-zero + stderr) → both stderr and exit-code error are surfaced."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print("fatal: segfault", file=sys.stderr, flush=True)
sys.exit(139)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is False
    assert stop_reason == "error_during_execution"
    # Stderr forwarded
    assert any("[stderr]" in line and "fatal" in line for line in emitted), emitted
    # Non-zero exit explicitly reported
    assert any("non-zero" in line for line in emitted), emitted


async def test_run_pi_auto_zero_exit_no_agent_end(tmp_path) -> None:
    """Pi exits 0 but never emits agent_end → not treated as success (no clean agent_end)."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_start"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    # exit_code == 0 but agent_ended_ok is False → success must be False
    assert success is False
    # stop_reason is "success" because exit_code==0 path sets it, but agent_ended_ok
    # gate prevents returning success=True
    assert stop_reason in ("success", "no_events")


# ---------------------------------------------------------------------------
# Goal 2: No "no-session" mode — session_id extraction
# ---------------------------------------------------------------------------


async def test_run_pi_auto_extracts_session_id_from_agent_start(tmp_path) -> None:
    """Session ID in agent_start is returned from run_pi_auto."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_start", "session_id": "sess-abc123"}}), flush=True)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is True
    assert session_id == "sess-abc123"


async def test_run_pi_auto_extracts_session_id_camel_case(tmp_path) -> None:
    """sessionId (camelCase) in agent_start is also accepted."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_start", "sessionId": "sess-xyz999"}}), flush=True)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is True
    assert session_id == "sess-xyz999"


async def test_run_pi_auto_no_session_flag_absent(tmp_path) -> None:
    """The --no-session flag must NOT appear in the pi command invocation."""
    fake_pi = tmp_path / "fake-pi"
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
# Fail if --no-session is passed so the test catches regression
if "--no-session" in sys.argv:
    print(json.dumps({{"type": "response", "command": "prompt", "success": False,
                       "error": "--no-session flag was passed"}}), flush=True)
    sys.exit(1)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is True, f"emitted: {emitted}"
    assert not any("--no-session" in line for line in emitted)


# ---------------------------------------------------------------------------
# Goal 3: Cost Tracking via on_usage callback
# ---------------------------------------------------------------------------


async def test_run_pi_auto_usage_from_agent_end(tmp_path) -> None:
    """Usage data in agent_end is forwarded to on_usage as a 'result' record."""
    fake_pi = tmp_path / "fake-pi"
    usage = {"inputTokens": 100, "outputTokens": 50}
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_end", "usage": {json.dumps(usage)}, "cost_usd": 0.0042, "model": "pi-1"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []
    usage_records: list[dict] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    async def on_usage(rec: dict) -> None:
        usage_records.append(rec)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi), on_usage=on_usage
    )

    assert success is True
    assert len(usage_records) == 1
    rec = usage_records[0]
    assert rec["kind"] == "result"
    assert rec["input_tokens"] == 100
    assert rec["output_tokens"] == 50
    assert rec["cost_usd"] == pytest.approx(0.0042)
    assert rec["model"] == "pi-1"


async def test_run_pi_auto_usage_from_message_update(tmp_path) -> None:
    """Per-message token counts in message_update are forwarded as 'api_call' records."""
    fake_pi = tmp_path / "fake-pi"
    usage = {"inputTokens": 20, "outputTokens": 10}
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{
    "type": "message_update",
    "usage": {json.dumps(usage)},
    "model": "pi-1",
    "message": {{"content": [{{"type": "text", "text": "hello"}}]}}
}}), flush=True)
print(json.dumps({{"type": "agent_end"}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []
    usage_records: list[dict] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    async def on_usage(rec: dict) -> None:
        usage_records.append(rec)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi), on_usage=on_usage
    )

    assert success is True
    api_calls = [r for r in usage_records if r.get("kind") == "api_call"]
    assert len(api_calls) == 1
    assert api_calls[0]["input_tokens"] == 20
    assert api_calls[0]["output_tokens"] == 10


async def test_run_pi_auto_no_usage_callback_still_works(tmp_path) -> None:
    """Passing no on_usage callback is fine — runner completes without error."""
    fake_pi = tmp_path / "fake-pi"
    usage = {"inputTokens": 5, "outputTokens": 3}
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_end", "usage": {json.dumps(usage)}}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )
    assert success is True


async def test_parse_pi_usage_zero_input_tokens_not_ignored() -> None:
    """inputTokens=0 must not fall through to input_tokens via or-chaining."""
    from pioneer_worker.pi_runner import _parse_pi_usage

    # If or-chaining is used, 0 (falsy) would cause fallthrough to input_tokens=99.
    event = {"usage": {"inputTokens": 0, "input_tokens": 99, "outputTokens": 5}}
    result = _parse_pi_usage(event)
    assert result is not None
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 5


async def test_run_pi_auto_no_double_error_when_saw_error(tmp_path) -> None:
    """When a message_update error is already forwarded, a non-zero exit must not
    emit a second error chunk (double-error bug)."""
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

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, _, _sid = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi)
    )

    assert success is False
    assert stop_reason == "error_during_execution"
    assert any("context_limit" in line for line in emitted)
    # No second error chunk about the non-zero exit code.
    assert not any("non-zero" in line for line in emitted), emitted


async def test_run_pi_auto_snake_case_usage_fields(tmp_path) -> None:
    """Snake_case usage fields (input_tokens/output_tokens) are parsed correctly."""
    fake_pi = tmp_path / "fake-pi"
    usage = {"input_tokens": 77, "output_tokens": 33}
    fake_pi.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "agent_end", "usage": {json.dumps(usage)}}}), flush=True)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)

    emitted: list[str] = []
    usage_records: list[dict] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    async def on_usage(rec: dict) -> None:
        usage_records.append(rec)

    success, stop_reason, last_text, session_id = await run_pi_auto(
        "do the work", str(tmp_path), emit=emit, pi_path=os.fspath(fake_pi), on_usage=on_usage
    )

    assert success is True
    assert len(usage_records) == 1
    assert usage_records[0]["input_tokens"] == 77
    assert usage_records[0]["output_tokens"] == 33
