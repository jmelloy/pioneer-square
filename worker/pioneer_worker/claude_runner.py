"""Run `claude --dangerously-skip-permissions` on a task and stream output."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable

from .log_format import strip_worktree_prefix

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]  # emit(line: str, detail: dict | None = None)
UsageFn = Callable[[dict], Awaitable[None]]  # on_usage(record: dict)


def _usage_tokens(usage: dict) -> dict:
    """Extract the four token counts from a message/result ``usage`` block."""
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


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


_THINKING_PREVIEW_LEN = 300


def _truncate_at_word(text: str, limit: int) -> str:
    """Truncate text at a word boundary at or before limit characters."""
    if len(text) <= limit:
        return text
    idx = text.rfind(" ", 0, limit)
    return text[: idx if idx > 0 else limit]


_TOOL_ACTIVITY: dict[str, str] = {
    "Bash": "running",
    "Read": "reading",
    "Write": "editing",
    "Edit": "editing",
    "MultiEdit": "editing",
    "NotebookEdit": "editing",
    "WebSearch": "searching",
    "WebFetch": "fetching",
    "ToolSearch": "searching",
    "Monitor": "reading",
    "ScheduleWakeup": "planning",
    "Skill": "planning",
    "Agent": "planning",
    "TodoWrite": "planning",
    "TodoRead": "reading",
}


def _stringify_tool_result_content(content: object) -> str:
    """Return a displayable string for Claude tool_result content.

    Recent Claude Code versions can return a list of content blocks here, not
    just a string.  Most list blocks are ``text``, but background/monitor-style
    tools can return lightweight ``tool_reference`` blocks; preserve those as a
    concise marker instead of silently dropping the result.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
        elif btype == "tool_reference":
            name = block.get("tool_name") or block.get("name") or "tool"
            parts.append(f"[tool reference: {name}]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _claude_json_detail(event: dict, *, block: dict | None = None) -> dict:
    detail: dict = {"toolType": "claude_json", "event": event}
    if block is not None:
        detail["block"] = block
    return detail


def _claude_json_summary(event: dict, *, block: dict | None = None) -> str:
    etype = event.get("type") or "event"
    subtype = event.get("subtype")
    label = f"{etype}:{subtype}" if subtype else str(etype)
    if block is not None:
        btype = block.get("type") or "block"
        label = f"{label}.{btype}"
    return f"[claude-json] {label}"


def parse_claude_event(event: dict) -> list[tuple[str, dict | None]]:
    """Extract (display_text, detail) pairs from one stream-JSON event.

    detail is sent as a separate tool-detail WS message so the full content
    is available on click without bloating the terminal-output stream.
    The detail dict includes an 'activity' key so the worker can track what
    Claude is currently doing and send granular agent-state updates.

    Unknown JSON events/blocks are still forwarded with their raw JSON under
    detail['event'] (and detail['block'] for content blocks) so frontend/backend
    logs don't silently lose new Claude Code stream shapes.
    """
    t = event.get("type")
    if t == "assistant":
        pairs: list[tuple[str, dict | None]] = []
        for blk in event.get("message", {}).get("content", []):
            if not isinstance(blk, dict):
                pairs.append((_claude_json_summary(event), _claude_json_detail(event)))
                continue
            btype = blk.get("type")
            if btype == "text":
                txt = blk.get("text", "").strip()
                if txt:
                    pairs.append((txt, None))
            elif btype == "thinking":
                thinking = blk.get("thinking", "").strip()
                if thinking:
                    flat = thinking.replace("\n", " ")
                    is_long = len(flat) > _THINKING_PREVIEW_LEN
                    preview = _truncate_at_word(flat, _THINKING_PREVIEW_LEN)
                    suffix = "..." if is_long else ""
                    detail: dict = {
                        "activity": "thinking",
                        "input": thinking,
                        "summary": preview,
                    }
                    if is_long:
                        detail["toolType"] = "thinking"
                        detail["fullText"] = thinking
                    pairs.append(
                        (
                            f"[thinking] {preview}{suffix}",
                            detail,
                        )
                    )
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
                    fp = strip_worktree_prefix(inp.get("file_path", inp.get("path", "")))
                    summary = f"▶ {name.lower()}: {fp}"
                else:
                    summary = f"▶ {name}: {json.dumps(inp)[:80]}"
                detail: dict = {"toolType": "tool_use", "name": name, "input": inp}
                activity = _TOOL_ACTIVITY.get(name)
                if activity:
                    detail["activity"] = activity
                pairs.append((summary, detail))
            else:
                pairs.append(
                    (_claude_json_summary(event, block=blk), _claude_json_detail(event, block=blk))
                )
        return pairs
    if t == "user":
        pairs = []
        content_blocks = event.get("message", {}).get("content", [])
        if isinstance(content_blocks, str):
            return [(_claude_json_summary(event), _claude_json_detail(event))]
        for blk in content_blocks:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                content = _stringify_tool_result_content(blk.get("content", ""))
                if content.strip():
                    lines = content.strip().splitlines()
                    summary = _summarize_lines(lines)
                    detail = {"toolType": "tool_result", "output": content}
                    pairs.append((summary, detail))
                else:
                    pairs.append(
                        (
                            _claude_json_summary(event, block=blk),
                            _claude_json_detail(event, block=blk),
                        )
                    )
            elif isinstance(blk, dict):
                pairs.append(
                    (_claude_json_summary(event, block=blk), _claude_json_detail(event, block=blk))
                )
            else:
                pairs.append((_claude_json_summary(event), _claude_json_detail(event)))
        return pairs
    if t == "result":
        subtype = event.get("subtype", "success")
        turns = event.get("num_turns", 0)
        cost = event.get("cost_usd", event.get("total_cost_usd"))
        cost_str = f" (${cost:.4f})" if isinstance(cost, int | float) and cost else ""
        if subtype == "success":
            return [(f"✓ Done in {turns} turns{cost_str}", None)]
        return [(f"✗ {subtype}: {event.get('error', '')}", None)]
    if t == "system" and event.get("subtype") == "init":
        tools = event.get("tools", [])
        return [(f"[claude] tools: {', '.join(tools[:6])}", None)]
    # Claude Code emits bookkeeping-only thinking token updates. They do not
    # contain displayable content, so avoid persisting noisy `[claude-json]`
    # fallback lines for future runs. Historical rows are hidden in the UI.
    if t == "system" and event.get("subtype") == "thinking_tokens":
        return []
    return [(_claude_json_summary(event), _claude_json_detail(event))]


class ClaudeProcess:
    """Wraps a running claude subprocess so the worker can inject stdin messages."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.session_id: str | None = None  # set once system:init is parsed

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


def _log_event_full(event: dict, pid: int, n: int) -> None:
    """Log the full content of a stream-json event, untruncated."""
    t = event.get("type")
    subtype = event.get("subtype")
    logger.debug("claude[%d] event#%d type=%s subtype=%s", pid, n, t, subtype)

    if t == "assistant":
        for i, blk in enumerate(event.get("message", {}).get("content", [])):
            btype = blk.get("type")
            if btype == "text":
                logger.debug(
                    "claude[%d] event#%d assistant.text[%d]:\n%s",
                    pid,
                    n,
                    i,
                    blk.get("text", ""),
                )
            elif btype == "tool_use":
                logger.debug(
                    "claude[%d] event#%d tool_use[%d] name=%s id=%s input=%s",
                    pid,
                    n,
                    i,
                    blk.get("name", ""),
                    blk.get("id", ""),
                    json.dumps(blk.get("input", {}), ensure_ascii=False),
                )
            elif btype == "thinking":
                logger.debug(
                    "claude[%d] event#%d assistant.thinking[%d]:\n%s",
                    pid,
                    n,
                    i,
                    blk.get("thinking", ""),
                )
            else:
                logger.debug(
                    "claude[%d] event#%d assistant.block[%d] type=%s: %s",
                    pid,
                    n,
                    i,
                    btype,
                    json.dumps(blk, ensure_ascii=False),
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
                    pid,
                    n,
                    i,
                    blk.get("tool_use_id", ""),
                    blk.get("is_error", False),
                    content,
                )
            else:
                logger.debug(
                    "claude[%d] event#%d user.block[%d] type=%s: %s",
                    pid,
                    n,
                    i,
                    btype,
                    json.dumps(blk, ensure_ascii=False),
                )
    elif t == "result":
        logger.debug(
            "claude[%d] event#%d result: %s",
            pid,
            n,
            json.dumps(event, ensure_ascii=False),
        )
    elif t == "system":
        logger.debug(
            "claude[%d] event#%d system: %s",
            pid,
            n,
            json.dumps(event, ensure_ascii=False),
        )
    else:
        logger.debug(
            "claude[%d] event#%d %s: %s",
            pid,
            n,
            t,
            json.dumps(event, ensure_ascii=False),
        )


async def run_claude_auto(
    description: str,
    cwd: str,
    *,
    max_turns: int,
    emit: EmitFn,
    on_proc: Callable[[ClaudeProcess], None] | None = None,
    on_usage: UsageFn | None = None,
    claude_path: str = "claude",
    resume_session_id: str | None = None,
    model: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, str, str | None]:
    """Run claude on *description* in *cwd*. Returns (success, stop_reason, last_assistant_text, session_id).

    stop_reason is the result event subtype: "success", "max_turns",
    "error_during_execution", "interrupted", or "no_events" when the process
    produced no stream-json output at all.

    If *resume_session_id* is given, passes ``--resume <id>`` so Claude continues
    the previous session with full context (used after a redirect/SIGTERM).

    If *model* is given, passes ``--model <model>`` to select a specific model.
    """
    cmd = [
        claude_path,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd += ["--model", model]
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
    session_id = None
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LINE_LIMIT,
            env=env,
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
            logger.debug(
                "claude[%d] stdout#%d (%d bytes): %s",
                proc.pid,
                event_count,
                len(line_str),
                line_str,
            )
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                logger.info("claude[%d] non-JSON stdout#%d: %s", proc.pid, event_count, line_str)
                await emit(line_str)
                continue
            _log_event_full(event, proc.pid, event_count)
            if event.get("type") == "system" and event.get("subtype") == "init":
                session_id = event.get("session_id")
                if session_id:
                    claude_proc.session_id = session_id
                    logger.info("claude[%d] session_id=%s", proc.pid, session_id)
            if event.get("type") == "result":
                stop_reason = event.get("subtype", "success")
            if on_usage is not None:
                etype = event.get("type")
                if etype == "assistant":
                    msg = event.get("message", {})
                    usage = msg.get("usage")
                    if isinstance(usage, dict):
                        await on_usage(
                            {
                                "kind": "api_call",
                                "model": msg.get("model"),
                                **_usage_tokens(usage),
                            }
                        )
                elif etype == "result":
                    usage = event.get("usage")
                    rec = {
                        "kind": "result",
                        "model": event.get("model"),
                        "cost_usd": event.get("total_cost_usd", event.get("cost_usd")),
                        "num_turns": event.get("num_turns"),
                        "stop_reason": event.get("subtype", "success"),
                        **(_usage_tokens(usage) if isinstance(usage, dict) else {}),
                    }
                    await on_usage(rec)
            for text, detail in parse_claude_event(event):
                await emit(text, detail)
                if not text.startswith(("▶", "✓", "✗", "[", "  →")):
                    last_text = text

        exit_code = await proc.wait()
        await stderr_task
        logger.info(
            "claude[%d] exited rc=%s stop_reason=%s after %d stdout event(s)",
            proc.pid,
            exit_code,
            stop_reason,
            event_count,
        )
        if event_count == 0:
            logger.warning(
                "claude[%d] produced no stdout events — check stderr above and PATH/auth",
                proc.pid,
            )
        success = exit_code == 0 and stop_reason == "success"
        return success, stop_reason, last_text, session_id
    except FileNotFoundError as exc:
        if not os.path.exists(claude_path):
            logger.error("claude executable not found: %r", claude_path)
            await emit(f"[claude] ✗ executable not found: {claude_path}")
        else:
            logger.error("claude failed to start (cwd missing?): %s — cwd=%r", exc, cwd)
            await emit(f"[claude] ✗ failed to start: {exc} (cwd={cwd!r})")
        return False, "no_events", last_text, session_id
    except Exception as exc:  # pragma: no cover
        logger.exception("claude subprocess crashed: %s", exc)
        await emit(f"[claude] ✗ {exc}")
        return False, "error_during_execution", last_text, session_id
    finally:
        # Anything that escapes the streaming loop — a WS send that gave up, a
        # stdout line past STDOUT_LINE_LIMIT — leaves claude running against the
        # worktree with nothing holding a reference to it. Reap it here; on the
        # normal path returncode is already set and this is a no-op.
        if proc is not None and proc.returncode is None:
            logger.warning("Reaping orphaned claude subprocess pid=%s", proc.pid)
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            except Exception as exc:  # pragma: no cover
                logger.debug("Failed to reap claude pid=%s: %s", proc.pid, exc)
