"""Run `codex exec --json` on a task and stream output."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

EmitFn = Callable[[str], Awaitable[None]]


def parse_codex_event(event: dict) -> str | None:
    """Extract a human-readable line from one codex stream-JSON event."""
    t = event.get("type")
    if t == "message" and event.get("role") == "assistant":
        return (event.get("content") or "").strip() or None
    if t == "function_call":
        name = event.get("name", "")
        args = event.get("arguments", "")
        return f"▶ {name}({args[:80]})"
    if t == "function_result":
        return f"  → {str(event.get('output', ''))[:200]}"
    if t == "done":
        return "✓ Done"
    if t == "error":
        return f"✗ {event.get('message', '')}"
    return None


async def run_codex_auto(
    description: str,
    cwd: str,
    *,
    emit: EmitFn,
    codex_path: str = "codex",
) -> tuple[bool, str, str]:
    """Run codex on *description* in *cwd*. Returns (success, stop_reason, last_text)."""
    cmd = [codex_path, "exec", "--json", description]
    logger.info("Spawning codex in %s; description=%r", cwd, description)
    logger.info("codex argv: %s", cmd)
    await emit(f"[codex] Starting: {description[:80]}")
    last_text = ""
    stop_reason = "no_events"
    event_count = 0
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("codex subprocess started pid=%s", proc.pid)

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
            logger.debug("codex[%d] stdout#%d: %s", proc.pid, event_count, line_str)
            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                await emit(line_str)
                continue
            if event.get("type") == "done":
                stop_reason = "success"
            elif event.get("type") == "error":
                stop_reason = "error_during_execution"
            text = parse_codex_event(event)
            if text:
                await emit(text)
                if not text.startswith(("▶", "✓", "✗", "  →")):
                    last_text = text

        exit_code = await proc.wait()
        await stderr_task
        if event_count == 0:
            logger.warning("codex[%d] produced no stdout events — check PATH/auth", proc.pid)
        if stop_reason == "no_events" and exit_code == 0:
            stop_reason = "success"
        return exit_code == 0, stop_reason, last_text
    except FileNotFoundError:
        logger.error("`codex` CLI not found on PATH")
        await emit("[codex] ✗ `codex` CLI not found on PATH")
        return False, "no_events", last_text
    except Exception as exc:  # pragma: no cover
        logger.exception("codex subprocess crashed: %s", exc)
        await emit(f"[codex] ✗ {exc}")
        return False, "error_during_execution", last_text
