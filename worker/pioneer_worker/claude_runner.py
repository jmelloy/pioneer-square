"""Run `claude --dangerously-skip-permissions` on a task and stream output."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]  # emit(line: str, detail: dict | None = None)


def _summarize_lines(lines: list[str], prefix: str = "  → ") -> str:
    """Format a list of lines as a 2+ellipsis+1 summary."""
    if len(lines) <= 4:
        return "\n".join(f"{prefix}{l}" for l in lines)
    middle = len(lines) - 3
    return (
        f"{prefix}{lines[0]}\n"
        f"{prefix}{lines[1]}\n"
        f"{prefix}… ({middle} more lines)\n"
        f"{prefix}{lines[-1]}"
    )


_TOOL_ACTIVITY: dict[str, str] = {
    "Bash": "running",
    "Read": "reading",
    "Write": "editing",
    "Edit": "editing",
    "MultiEdit": "editing",
    "NotebookEdit": "editing",
    "WebSearch": "searching",
    "WebFetch": "fetching",
    "Agent": "planning",
    "TodoWrite": "planning",
    "TodoRead": "reading",
}


def parse_claude_event(event: dict) -> list[tuple[str, Optional[dict]]]:
    """Extract (display_text, detail) pairs from one stream-JSON event.

    detail is sent as a separate tool-detail WS message so the full content
    is available on click without bloating the terminal-output stream.
    The detail dict includes an 'activity' key so the worker can track what
    Claude is currently doing and send granular agent-state updates.
    """
    t = event.get("type")
    if t == "assistant":
        pairs: list[tuple[str, Optional[dict]]] = []
        for blk in event.get("message", {}).get("content", []):
            btype = blk.get("type")
            if btype == "text":
                txt = blk.get("text", "").strip()
                if txt:
                    pairs.append((txt, None))
            elif btype == "thinking":
                thinking = blk.get("thinking", "").strip()
                if thinking:
                    preview = thinking[:100].replace("\n", " ")
                    pairs.append((
                        f"[thinking] {preview}{'...' if len(thinking) > 100 else ''}",
                        {"activity": "thinking"},
                    ))
            elif btype == "tool_use":
                name = blk.get("name", "")
                inp = blk.get("input", {})
                if name == "Bash":
                    cmd = inp.get("command", "")
                    cmd_lines = [l for l in cmd.splitlines() if l.strip()]
                    if len(cmd_lines) <= 1:
                        summary = f"▶ bash: {cmd[:160]}"
                    else:
                        summary = f"▶ bash: {cmd_lines[0][:120]}\n         {cmd_lines[1][:120]}"
                        if len(cmd_lines) > 2:
                            summary += f"\n         … ({len(cmd_lines) - 2} more lines)"
                elif name in ("Read", "Write", "Edit"):
                    fp = inp.get("file_path", inp.get("path", ""))
                    summary = f"▶ {name.lower()}: {fp}"
                else:
                    summary = f"▶ {name}: {json.dumps(inp)[:80]}"
                detail: dict = {"toolType": "tool_use", "name": name, "input": inp}
                activity = _TOOL_ACTIVITY.get(name)
                if activity:
                    detail["activity"] = activity
                pairs.append((summary, detail))
        return pairs
    if t == "user":
        pairs = []
        for blk in event.get("message", {}).get("content", []):
            if blk.get("type") == "tool_result":
                content = blk.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
                if isinstance(content, str) and content.strip():
                    lines = content.strip().splitlines()
                    summary = _summarize_lines(lines)
                    detail = {"toolType": "tool_result", "output": content}
                    pairs.append((summary, detail))
        return pairs
    if t == "result":
        subtype = event.get("subtype", "success")
        turns = event.get("num_turns", 0)
        cost = event.get("cost_usd")
        cost_str = f" (${cost:.4f})" if cost else ""
        if subtype == "success":
            return [(f"✓ Done in {turns} turns{cost_str}", None)]
        return [(f"✗ {subtype}: {event.get('error', '')}", None)]
    if t == "system" and event.get("subtype") == "init":
        tools = event.get("tools", [])
        return [(f"[claude] tools: {', '.join(tools[:6])}", None)]
    return []


class ClaudeProcess:
    """Wraps a running claude subprocess so the worker can inject stdin messages."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.session_id: Optional[str] = None  # set once system:init is parsed

    async def send_message(self, text: str) -> bool:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            return False
        try:
            self.proc.stdin.write((text + "\n").encode())
            await self.proc.stdin.drain()
            return True
        except Exception:
            return False

    async def terminate(self) -> None:
        """Terminate the claude subprocess (SIGTERM, then SIGKILL if needed)."""
        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.debug("terminate() error: %s", exc)


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
                logger.debug(
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
                logger.debug(
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
    claude_path: str = "claude",
    resume_session_id: Optional[str] = None,
) -> tuple[bool, str, str]:
    """Run claude on *description* in *cwd*. Returns (success, stop_reason, last_assistant_text).

    stop_reason is the result event subtype: "success", "max_turns",
    "error_during_execution", "interrupted", or "no_events" when the process
    produced no stream-json output at all.

    If *resume_session_id* is given, passes ``--resume <id>`` so Claude continues
    the previous session with full context (used after a redirect/SIGTERM).
    """
    cmd = [
        claude_path,
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(max_turns),
        "--dangerously-skip-permissions",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id, "-p", description]
    else:
        cmd += ["-p", description]
    logger.info("Spawning claude in %s; description=%r", cwd, description)
    logger.info("claude argv: %s", cmd)
    await emit(f"[claude] Starting: {description[:80]}")
    last_text = ""
    stop_reason = "no_events"
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
        claude_proc = ClaudeProcess(proc)
        if on_proc is not None:
            on_proc(claude_proc)

        async def _drain_stderr() -> None:
            async for raw in proc.stderr:  # type: ignore[union-attr]
                line = raw.decode(errors="replace").strip()
                if line:
                    await emit(f"[stderr] {line}")

        stderr_task = asyncio.create_task(_drain_stderr())

        async for raw in proc.stdout:  # type: ignore[union-attr]
            line_str = raw.decode(errors="replace").strip()
            if not line_str:
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
            if event.get("type") == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                if sid:
                    claude_proc.session_id = sid
                    logger.info("claude[%d] session_id=%s", proc.pid, sid)
            if event.get("type") == "result":
                stop_reason = event.get("subtype", "success")
            for text, detail in parse_claude_event(event):
                await emit(text, detail)
                if not text.startswith(("▶", "✓", "✗", "[", "  →")):
                    last_text = text

        exit_code = await proc.wait()
        await stderr_task
        logger.info(
            "claude[%d] exited rc=%s stop_reason=%s after %d stdout event(s)",
            proc.pid, exit_code, stop_reason, event_count,
        )
        if event_count == 0:
            logger.warning(
                "claude[%d] produced no stdout events — check stderr above and PATH/auth",
                proc.pid,
            )
        return exit_code == 0, stop_reason, last_text
    except FileNotFoundError as exc:
        if not os.path.exists(claude_path):
            logger.error("claude executable not found: %r", claude_path)
            await emit(f"[claude] ✗ executable not found: {claude_path}")
        else:
            logger.error("claude failed to start (cwd missing?): %s — cwd=%r", exc, cwd)
            await emit(f"[claude] ✗ failed to start: {exc} (cwd={cwd!r})")
        return False, "no_events", last_text
    except Exception as exc:  # pragma: no cover
        logger.exception("claude subprocess crashed: %s", exc)
        await emit(f"[claude] ✗ {exc}")
        return False, "error_during_execution", last_text
