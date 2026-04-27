"""Run `claude --dangerously-skip-permissions` on a task and stream output."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

EmitFn = Callable[[str], Awaitable[None]]


def parse_claude_event(event: dict) -> Optional[str]:
    """Extract a human-readable line from one stream-JSON event."""
    t = event.get("type")
    if t == "assistant":
        parts: list[str] = []
        for blk in event.get("message", {}).get("content", []):
            if blk.get("type") == "text":
                txt = blk.get("text", "").strip()
                if txt:
                    parts.append(txt)
            elif blk.get("type") == "tool_use":
                name = blk.get("name", "")
                inp = blk.get("input", {})
                if name == "Bash":
                    parts.append(f"▶ bash: {inp.get('command', '')[:120]}")
                elif name in ("Read", "Write", "Edit"):
                    fp = inp.get("file_path", inp.get("path", ""))
                    parts.append(f"▶ {name.lower()}: {fp}")
                else:
                    parts.append(f"▶ {name}: {json.dumps(inp)[:80]}")
        return "\n".join(parts) or None
    if t == "result":
        subtype = event.get("subtype", "success")
        turns = event.get("num_turns", 0)
        cost = event.get("cost_usd")
        cost_str = f" (${cost:.4f})" if cost else ""
        if subtype == "success":
            return f"✓ Done in {turns} turns{cost_str}"
        return f"✗ {subtype}: {event.get('error', '')}"
    if t == "system" and event.get("subtype") == "init":
        tools = event.get("tools", [])
        return f"[claude] tools: {', '.join(tools[:6])}"
    return None


class ClaudeProcess:
    """Wraps a running claude subprocess so the worker can inject stdin messages."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    async def send_message(self, text: str) -> bool:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            return False
        try:
            self.proc.stdin.write((text + "\n").encode())
            await self.proc.stdin.drain()
            return True
        except Exception:
            return False


async def _drain_stderr(stream, pid: int) -> None:
    """Log claude's stderr line-by-line so we can diagnose silent failures."""
    try:
        async for raw in stream:
            line = raw.decode(errors="replace").rstrip()
            if line:
                logger.warning("claude[%d] stderr: %s", pid, line[:500])
    except Exception as exc:  # pragma: no cover
        logger.debug("claude[%d] stderr drain error: %s", pid, exc)


async def run_claude_auto(
    description: str,
    cwd: str,
    *,
    max_turns: int,
    emit: EmitFn,
    on_proc: Optional[Callable[[ClaudeProcess], None]] = None,
) -> tuple[bool, str]:
    """Run claude on *description* in *cwd*. Returns (success, last_assistant_text)."""
    cmd = [
        "claude",
        "--output-format", "stream-json",
        "--max-turns", str(max_turns),
        "--dangerously-skip-permissions",
        "-p", description,
    ]
    logger.info("Spawning claude in %s: %s", cwd, " ".join(cmd[:5] + ["…"]))
    await emit(f"[claude] Starting: {description[:80]}")
    last_text = ""
    event_count = 0
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("claude subprocess started pid=%s", proc.pid)
        if on_proc is not None:
            on_proc(ClaudeProcess(proc))

        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, proc.pid))  # type: ignore[arg-type]

        async for raw in proc.stdout:  # type: ignore[union-attr]
            line_str = raw.decode(errors="replace").strip()
            if not line_str:
                continue
            event_count += 1
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                logger.debug("claude[%d] non-JSON stdout: %s", proc.pid, line_str[:200])
                await emit(line_str)
                continue
            logger.debug(
                "claude[%d] event #%d type=%s subtype=%s",
                proc.pid, event_count, event.get("type"), event.get("subtype"),
            )
            text = parse_claude_event(event)
            if text:
                await emit(text)
                if not text.startswith(("▶", "✓", "✗", "[")):
                    last_text = text

        exit_code = await proc.wait()
        await stderr_task
        logger.info(
            "claude[%d] exited rc=%s after %d event(s)",
            proc.pid, exit_code, event_count,
        )
        if event_count == 0:
            logger.warning(
                "claude[%d] produced no stdout events — check stderr above and PATH/auth",
                proc.pid,
            )
        return exit_code == 0, last_text
    except FileNotFoundError:
        logger.error("`claude` CLI not found on PATH")
        await emit("[claude] ✗ `claude` CLI not found on PATH")
        return False, last_text
    except Exception as exc:  # pragma: no cover
        logger.exception("claude subprocess crashed: %s", exc)
        await emit(f"[claude] ✗ {exc}")
        return False, last_text
