"""Run `pi --mode rpc --no-session` on a task and stream output."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

EmitFn = Callable[[str], Awaitable[None]]

# Seconds to wait for pi to exit after stdin is closed before killing it.
_WAIT_TIMEOUT = 30


def _result_text(result: dict) -> str:
    """Join the text blocks of a tool result's content array."""
    parts = []
    for blk in result.get("content", []):
        if isinstance(blk, dict) and blk.get("type") == "text":
            parts.append(blk.get("text", ""))
    return "".join(parts)


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
    pi_path: str = "pi",
) -> tuple[bool, str, str]:
    """Run pi on *description* in *cwd*. Returns (success, stop_reason, last_text)."""
    cmd = [pi_path, "--mode", "rpc", "--no-session"]
    logger.info("Spawning pi in %s; description=%r", cwd, description)
    saw_agent_end = False
    saw_error = False
    logger.info("pi argv: %s", cmd)
    await emit(f"[pi] Starting: {description[:80]}")
    last_text = ""
    stop_reason = "no_events"
    event_count = 0
    agent_ended_ok = False
    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
            except Exception:
                logger.debug("pi[%d] stderr drain exited early", pid, exc_info=True)

        stderr_task = asyncio.create_task(_drain_stderr())
        accumulated = ""

        async for raw in proc.stdout:  # type: ignore[union-attr]
            line_str = raw.decode(errors="replace").strip()
            if not line_str:
                continue
            event_count += 1
            logger.debug("pi[%d] stdout#%d: %s", proc.pid, event_count, line_str)
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await emit(line_str)
                continue
            etype = event.get("type")
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
                if accumulated.strip():
                    last_text = accumulated
                accumulated = ""
            if etype == "message_update":
                ame = event.get("assistantMessageEvent", {})
                if ame.get("type") == "error":
                    saw_error = True
                    reason = ame.get("reason", "error")
                    await emit(f"[pi] ✗ {reason}")
            text, accumulated = parse_pi_event(event, accumulated)
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
        except (TimeoutError, asyncio.TimeoutError):
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
        agent_ended_ok = saw_agent_end and not saw_error
        return (exit_code == 0 and agent_ended_ok), stop_reason, last_text
    except FileNotFoundError:
        logger.error("`pi` CLI not found on PATH")
        await emit("[pi] ✗ `pi` CLI not found on PATH")
        return False, "no_events", last_text
    except Exception as exc:  # pragma: no cover
        logger.exception("pi subprocess crashed: %s", exc)
        await emit(f"[pi] ✗ {exc}")
        return False, "error_during_execution", last_text
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
