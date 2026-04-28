"""Run `pi --mode rpc --no-session` on a task and stream output."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

EmitFn = Callable[[str], Awaitable[None]]


def parse_pi_event(event: dict, last_text: str) -> tuple[Optional[str], str]:
    """Extract a human-readable line from one pi RPC event.

    Returns (display_text_or_None, updated_last_text).
    """
    t = event.get("type")
    if t == "message_update":
        full = ""
        for blk in event.get("message", {}).get("content", []):
            if isinstance(blk, dict) and blk.get("type") == "text":
                full += blk.get("text", "")
        delta = full[len(last_text):]
        return (delta if delta.strip() else None), full
    if t == "tool_execution_start":
        ti = event.get("tool", {})
        name = ti.get("name", "")
        inp = ti.get("input", {})
        if name == "bash":
            return f"▶ bash: {inp.get('command', '')[:120]}", last_text
        if name in ("read", "write", "edit"):
            return f"▶ {name}: {inp.get('path', inp.get('file_path', ''))}", last_text
        return f"▶ {name}({json.dumps(inp)[:80]})", last_text
    if t == "tool_execution_end":
        out = str(event.get("output", "")).strip()
        if not out:
            return None, last_text
        lines = out.split("\n")
        preview = lines[0][:120]
        if len(lines) > 1:
            preview += f" (+{len(lines) - 1} lines)"
        return f"  → {preview}", last_text
    if t == "agent_end":
        err = event.get("error")
        return (f"✗ {err}" if err else None), ""
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
    logger.info("pi argv: %s", cmd)
    await emit(f"[pi] Starting: {description[:80]}")
    last_text = ""
    stop_reason = "no_events"
    event_count = 0
    agent_ended_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("pi subprocess started pid=%s", proc.pid)

        rpc_msg = json.dumps({"type": "prompt", "content": description}) + "\n"
        proc.stdin.write(rpc_msg.encode())  # type: ignore[union-attr]
        await proc.stdin.drain()  # type: ignore[union-attr]

        async def _drain_stderr() -> None:
            async for raw in proc.stderr:  # type: ignore[union-attr]
                line = raw.decode(errors="replace").strip()
                if line:
                    await emit(f"[stderr] {line}")

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
            if event.get("type") == "agent_end":
                stop_reason = "error_during_execution" if event.get("error") else "success"
                agent_ended_ok = not event.get("error")
            text, accumulated = parse_pi_event(event, accumulated)
            if text:
                await emit(text)
                if not text.startswith(("▶", "✗", "  →", "[pi]")):
                    last_text = text

        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()
        exit_code = await proc.wait()
        await stderr_task
        if event_count == 0:
            logger.warning("pi[%d] produced no stdout events — check PATH/auth", proc.pid)
        if stop_reason == "no_events" and exit_code == 0:
            stop_reason = "success"
        return (exit_code == 0 and agent_ended_ok), stop_reason, last_text
    except FileNotFoundError:
        logger.error("`pi` CLI not found on PATH")
        await emit("[pi] ✗ `pi` CLI not found on PATH")
        return False, "no_events", last_text
    except Exception as exc:  # pragma: no cover
        logger.exception("pi subprocess crashed: %s", exc)
        await emit(f"[pi] ✗ {exc}")
        return False, "error_during_execution", last_text
