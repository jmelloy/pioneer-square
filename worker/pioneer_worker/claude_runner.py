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


# Allow long stream-json lines (large tool_result payloads). The asyncio default
# StreamReader limit is 64 KiB which is easily exceeded by file reads.
STDOUT_LINE_LIMIT = 16 * 1024 * 1024  # 16 MiB


async def _drain_stderr(stream, pid: int) -> int:
    """Log claude's stderr line-by-line at full length. Returns line count."""
    n = 0
    try:
        while True:
            try:
                raw = await stream.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                raw = exc.partial
                if not raw:
                    break
            except asyncio.LimitOverrunError as exc:
                # Line longer than the buffer — pull what we have and keep going.
                raw = await stream.readexactly(exc.consumed)
            except (asyncio.CancelledError, ValueError):
                break
            line = raw.decode(errors="replace").rstrip("\n")
            if line:
                n += 1
                logger.warning("claude[%d] stderr: %s", pid, line)
            if not raw:
                break
    except Exception as exc:  # pragma: no cover
        logger.debug("claude[%d] stderr drain error: %s", pid, exc)
    logger.info("claude[%d] stderr drain finished after %d line(s)", pid, n)
    return n


def _log_event_full(event: dict, pid: int, n: int) -> None:
    """Log the full content of a stream-json event, untruncated."""
    t = event.get("type")
    subtype = event.get("subtype")
    logger.info("claude[%d] event#%d type=%s subtype=%s", pid, n, t, subtype)

    if t == "assistant":
        for i, blk in enumerate(event.get("message", {}).get("content", [])):
            btype = blk.get("type")
            if btype == "text":
                logger.info(
                    "claude[%d] event#%d assistant.text[%d]:\n%s",
                    pid, n, i, blk.get("text", ""),
                )
            elif btype == "tool_use":
                logger.info(
                    "claude[%d] event#%d tool_use[%d] name=%s id=%s input=%s",
                    pid, n, i, blk.get("name", ""), blk.get("id", ""),
                    json.dumps(blk.get("input", {}), ensure_ascii=False),
                )
            elif btype == "thinking":
                logger.info(
                    "claude[%d] event#%d assistant.thinking[%d]:\n%s",
                    pid, n, i, blk.get("thinking", ""),
                )
            else:
                logger.info(
                    "claude[%d] event#%d assistant.block[%d] type=%s: %s",
                    pid, n, i, btype, json.dumps(blk, ensure_ascii=False),
                )
    elif t == "user":
        for i, blk in enumerate(event.get("message", {}).get("content", [])):
            btype = blk.get("type")
            if btype == "tool_result":
                content = blk.get("content")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                logger.info(
                    "claude[%d] event#%d tool_result[%d] tool_use_id=%s is_error=%s:\n%s",
                    pid, n, i, blk.get("tool_use_id", ""),
                    blk.get("is_error", False), content,
                )
            else:
                logger.info(
                    "claude[%d] event#%d user.block[%d] type=%s: %s",
                    pid, n, i, btype, json.dumps(blk, ensure_ascii=False),
                )
    elif t == "result":
        logger.info(
            "claude[%d] event#%d result: %s",
            pid, n, json.dumps(event, ensure_ascii=False),
        )
    elif t == "system":
        logger.info(
            "claude[%d] event#%d system: %s",
            pid, n, json.dumps(event, ensure_ascii=False),
        )
    else:
        logger.info(
            "claude[%d] event#%d %s: %s",
            pid, n, t, json.dumps(event, ensure_ascii=False),
        )


async def _iter_stdout_lines(stream):
    """Yield stdout lines without the 64KiB asyncio default line cap."""
    while True:
        try:
            raw = await stream.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                yield exc.partial
            return
        except asyncio.LimitOverrunError as exc:
            # Line is longer than the StreamReader buffer; pull what's queued and
            # keep reading more chunks until we see a newline or EOF.
            chunks = [await stream.readexactly(exc.consumed)]
            while True:
                try:
                    more = await stream.readuntil(b"\n")
                    chunks.append(more)
                    break
                except asyncio.IncompleteReadError as exc2:
                    if exc2.partial:
                        chunks.append(exc2.partial)
                    yield b"".join(chunks)
                    return
                except asyncio.LimitOverrunError as exc2:
                    chunks.append(await stream.readexactly(exc2.consumed))
            yield b"".join(chunks)
            continue
        if not raw:
            return
        yield raw


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
    logger.info("Spawning claude in %s; description=%r", cwd, description)
    logger.info("claude argv: %s", cmd)
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
            limit=STDOUT_LINE_LIMIT,
        )
        logger.info("claude subprocess started pid=%s", proc.pid)
        if on_proc is not None:
            on_proc(ClaudeProcess(proc))

        stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, proc.pid))  # type: ignore[arg-type]

        async for raw in _iter_stdout_lines(proc.stdout):  # type: ignore[arg-type]
            line_str = raw.decode(errors="replace").rstrip("\n")
            if not line_str.strip():
                continue
            event_count += 1
            # Always log the full raw line at DEBUG so the wire format is recoverable.
            logger.debug("claude[%d] stdout#%d (%d bytes): %s",
                         proc.pid, event_count, len(line_str), line_str)
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                logger.info("claude[%d] non-JSON stdout#%d: %s",
                            proc.pid, event_count, line_str)
                await emit(line_str)
                continue
            _log_event_full(event, proc.pid, event_count)
            text = parse_claude_event(event)
            if text:
                await emit(text)
                if not text.startswith(("▶", "✓", "✗", "[")):
                    last_text = text

        exit_code = await proc.wait()
        await stderr_task
        logger.info(
            "claude[%d] exited rc=%s after %d stdout event(s)",
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
