"""Run `pi --mode rpc` on a task and stream output."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

EmitFn = Callable[[str], Awaitable[None]]
UsageFn = Callable[[dict], Awaitable[None]]  # on_usage(record: dict)

# Seconds to wait for pi to exit after stdin is closed before killing it.
_WAIT_TIMEOUT = 30

# StreamReader buffer size — large enough to handle big tool outputs.
_STREAM_LIMIT = 10 * 1024 * 1024  # 10 MB


def _result_text(result: dict) -> str:
    """Join the text blocks of a tool result's content array."""
    parts = []
    for blk in result.get("content", []):
        if isinstance(blk, dict) and blk.get("type") == "text":
            parts.append(blk.get("text", ""))
    return "".join(parts)


def _parse_pi_usage(event: dict) -> dict | None:
    """Extract token counts from a pi event's usage block, if present.

    Pi may use camelCase or snake_case field names depending on version.
    Returns None when no usage block is found.
    """
    usage = event.get("usage") or event.get("tokenUsage")
    if not isinstance(usage, dict):
        return None
    v = usage.get("inputTokens")
    input_tokens = v if v is not None else usage.get("input_tokens", 0)
    v = usage.get("outputTokens")
    output_tokens = v if v is not None else usage.get("output_tokens", 0)
    v = usage.get("cacheReadInputTokens")
    cache_read = v if v is not None else usage.get("cache_read_input_tokens", 0)
    v = usage.get("cacheCreationInputTokens")
    cache_creation = v if v is not None else usage.get("cache_creation_input_tokens", 0)
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_read_input_tokens": int(cache_read),
        "cache_creation_input_tokens": int(cache_creation),
    }


def parse_pi_event(event: dict, last_text: str) -> tuple[str | None, str]:
    """Extract a human-readable line from one pi RPC event.

    Returns (display_text_or_None, updated_last_text).
    """
    t = event.get("type")
    if t == "message_update":
        full = ""
        for blk in event.get("message", {}).get("content", []):
            if isinstance(blk, dict) and blk.get("type") == "text":
                full += blk.get("text", "")
        delta = full[len(last_text) :]
        return (delta if delta.strip() else None), full
    if t == "tool_execution_start":
        name = event.get("toolName", "")
        inp = event.get("args", {})
        if name == "bash":
            return f"▶ bash: {inp.get('command', '')[:120]}", last_text
        if name in ("read", "write", "edit"):
            return f"▶ {name}: {inp.get('path', inp.get('file_path', ''))}", last_text
        return f"▶ {name}({json.dumps(inp)[:80]})", last_text
    if t == "tool_execution_end":
        out = _result_text(event.get("result", {})).strip()
        if not out:
            return None, last_text
        lines = out.split("\n")
        preview = lines[0][:120]
        if len(lines) > 1:
            preview += f" (+{len(lines) - 1} lines)"
        prefix = "  ✗ " if event.get("isError") else "  → "
        return f"{prefix}{preview}", last_text
    if t == "agent_start":
        return "[pi] agent started", last_text
    return None, last_text


async def run_pi_auto(
    description: str,
    cwd: str,
    *,
    emit: EmitFn,
    on_usage: UsageFn | None = None,
    pi_path: str = "pi",
    model: str | None = None,
    provider: str | None = None,
    raw_log_path: str | None = None,
) -> tuple[bool, str, str, str | None]:
    """Run pi on *description* in *cwd*.

    Returns (success, stop_reason, last_text, session_id).

    Session tracking is enabled by default (no ``--no-session`` flag).
    Pi emits a ``session_id`` field in ``agent_start`` (or a dedicated
    ``session`` event); the value is extracted and returned so callers can
    resume or reference the session.

    If *on_usage* is provided it is called with usage-record dicts following
    the same shape used by claude_runner:
      - ``{"kind": "api_call", "model": ..., "input_tokens": ..., ...}`` for
        per-message token counts emitted in ``message_update`` events.
      - ``{"kind": "result", "model": ..., "cost_usd": ..., ...}`` for the
        final summary emitted in ``agent_end``.
    """
    cmd = [pi_path, "--mode", "rpc"]
    if provider:
        cmd += ["--provider", provider]
    if model:
        cmd += ["--model", model]
    logger.info("Spawning pi in %s; description=%r", cwd, description)
    saw_agent_end = False
    saw_error = False
    logger.info("pi argv: %s", cmd)
    await emit(f"[pi] Starting: {description[:80]}")
    last_text = ""
    stop_reason = "no_events"
    event_count = 0
    agent_ended_ok = False
    session_id: str | None = None
    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    _raw_fh = None
    if raw_log_path:
        try:
            _raw_fh = open(raw_log_path, "ab")  # noqa: WPS515
        except OSError as exc:
            logger.warning("Could not open raw log %s: %s", raw_log_path, exc)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        logger.info("pi subprocess started pid=%s", proc.pid)

        rpc_msg = json.dumps({"type": "prompt", "message": description}) + "\n"
        proc.stdin.write(rpc_msg.encode())  # type: ignore[union-attr]
        await proc.stdin.drain()  # type: ignore[union-attr]

        pid = proc.pid

        async def _drain_stderr() -> None:
            try:
                async for raw in proc.stderr:  # type: ignore[union-attr]
                    line = raw.decode(errors="replace").strip()
                    if line:
                        await emit(f"[stderr] {line}")
                    if _raw_fh is not None:
                        try:
                            _raw_fh.write(b"[stderr] " + raw if raw.endswith(b"\n") else b"[stderr] " + raw + b"\n")
                            _raw_fh.flush()
                        except OSError:
                            pass
            except Exception:
                logger.debug("pi[%d] stderr drain exited early", pid, exc_info=True)

        stderr_task = asyncio.create_task(_drain_stderr())
        accumulated = ""
        # Buffer for incomplete lines from message_update streaming deltas.
        # Pi streams token-by-token so raw deltas arrive as word-sized chunks;
        # we buffer and emit only at newline boundaries so each terminal-output
        # message is a complete line rather than a single word.
        _text_buf = ""

        async def _flush_text_buf() -> None:
            nonlocal _text_buf
            if _text_buf.strip():
                await emit(_text_buf.strip())
            _text_buf = ""

        while True:
            try:
                raw = await proc.stdout.readline()  # type: ignore[union-attr]
            except (asyncio.LimitOverrunError, ValueError) as exc:
                logger.error(
                    "pi[%d] stdout line exceeded StreamReader limit, skipping line: %s",
                    proc.pid,
                    exc,
                )
                await emit(f"[pi] ✗ stdout line too large, skipping: {exc}")
                # Drain the rest of the oversized line one byte at a time so we
                # stop exactly at the \n and don't consume bytes from the next line.
                try:
                    while True:
                        byte = await proc.stdout.read(1)  # type: ignore[union-attr]
                        if not byte or byte == b"\n":
                            break
                except Exception:
                    pass
                continue
            if not raw:  # EOF
                break
            if _raw_fh is not None:
                try:
                    _raw_fh.write(raw)
                    _raw_fh.flush()
                except OSError:
                    pass
            line_str = raw.decode(errors="replace").strip()
            if not line_str:
                continue
            event_count += 1
            logger.debug("pi[%d] stdout#%d: %s", proc.pid, event_count, line_str)
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await _flush_text_buf()
                await emit(line_str)
                continue
            etype = event.get("type")

            # Extract session_id from agent_start or a dedicated session event.
            if etype in ("agent_start", "session"):
                sid = event.get("session_id") or event.get("sessionId")
                if sid:
                    session_id = sid
                    logger.info("pi[%d] session_id=%s", proc.pid, session_id)

            if (
                etype == "response"
                and event.get("command") == "prompt"
                and not event.get("success", True)
            ):
                # Prompt was rejected before acceptance.
                err = event.get("error", "prompt rejected")
                await emit(f"[pi] ✗ {err}")
                saw_error = True
            if etype == "agent_end":
                saw_agent_end = True
                await _flush_text_buf()
                if accumulated.strip():
                    last_text = accumulated
                accumulated = ""
                # Emit final usage record if pi includes cost/token data here.
                if on_usage is not None:
                    usage_data = _parse_pi_usage(event)
                    cost = event.get("cost_usd") or event.get("costUsd")
                    if usage_data or cost is not None:
                        rec: dict = {
                            "kind": "result",
                            "model": event.get("model"),
                            "cost_usd": cost,
                            "stop_reason": "success",
                            **(usage_data or {}),
                        }
                        await on_usage(rec)
            if etype == "message_update":
                ame = event.get("assistantMessageEvent", {})
                if ame.get("type") == "error":
                    saw_error = True
                    reason = ame.get("reason", "error")
                    await emit(f"[pi] ✗ {reason}")
                # Emit per-message token counts if present.
                if on_usage is not None:
                    usage_data = _parse_pi_usage(event)
                    if usage_data:
                        await on_usage(
                            {
                                "kind": "api_call",
                                "model": event.get("model"),
                                **usage_data,
                            }
                        )
            text, accumulated = parse_pi_event(event, accumulated)
            if etype == "message_update":
                # Buffer streaming text; emit only at newline boundaries so
                # each terminal-output message carries a full line, not a token.
                if text:
                    _text_buf += text
                while "\n" in _text_buf:
                    pos = _text_buf.index("\n")
                    line = _text_buf[:pos]
                    if line.strip():
                        await emit(line)
                    _text_buf = _text_buf[pos + 1 :]
            else:
                # Non-streaming event: flush any buffered text first so ordering
                # is preserved, then emit the formatted summary line.
                await _flush_text_buf()
                if text:
                    await emit(text)
            if etype == "message_update" and accumulated.strip():
                last_text = accumulated
            # We only send a single prompt; pi RPC mode stays alive waiting
            # for more stdin after the run completes, so stop reading once the
            # agent (or a fatal error) has finished.
            if saw_agent_end or saw_error:
                break

        # Closing stdin signals EOF so the RPC process exits cleanly.
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=_WAIT_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "pi[%d] did not exit within %ds after stdin close; killing",
                proc.pid,
                _WAIT_TIMEOUT,
            )
            proc.kill()
            exit_code = await proc.wait()
        await stderr_task
        if event_count == 0:
            logger.warning("pi[%d] produced no stdout events — check PATH/auth", proc.pid)
        if saw_error:
            stop_reason = "error_during_execution"
        elif saw_agent_end:
            stop_reason = "success"
        elif exit_code == 0:
            stop_reason = "success"
        # Emit an explicit error when the subprocess exits non-zero without a
        # clean agent_end so callers see a failure rather than a silent EOF.
        # Guard with `not saw_error` to avoid emitting a second error when the
        # message_update handler already forwarded one.
        agent_ended_ok = saw_agent_end and not saw_error
        if exit_code != 0 and not agent_ended_ok and not saw_error:
            msg = f"[pi] ✗ process exited with non-zero code {exit_code}"
            logger.error("pi[%d] %s", proc.pid if proc else "?", msg)
            await emit(msg)
        if exit_code != 0 and not agent_ended_ok:
            stop_reason = "error_during_execution"
        return (exit_code == 0 and agent_ended_ok), stop_reason, last_text, session_id
    except FileNotFoundError:
        logger.error("`pi` CLI not found on PATH")
        await emit("[pi] ✗ `pi` CLI not found on PATH")
        return False, "no_events", last_text, session_id
    except Exception as exc:  # pragma: no cover
        logger.exception("pi subprocess crashed: %s", exc)
        await emit(f"[pi] ✗ {exc}")
        return False, "error_during_execution", last_text, session_id
    finally:
        # Safety net: ensure the subprocess is gone even if we took an
        # exception or were cancelled before the normal cleanup above ran.
        if proc is not None and proc.returncode is None:
            if proc.stdin and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                logger.debug("pi kill failed", exc_info=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except BaseException:
                logger.debug("pi wait-after-kill failed", exc_info=True)
        # Cancel the stderr drain task if it is still running.
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except BaseException:
                pass
        if _raw_fh is not None:
            try:
                _raw_fh.close()
            except OSError:
                pass
