"""Run `codex exec --json` on a task and stream output."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pty
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)

EmitFn = Callable[..., Awaitable[None]]  # emit(line: str, detail: dict | None = None)


def parse_codex_event(event: dict) -> tuple[str | None, dict | None]:
    """Extract (display_text, detail) from one codex stream-JSON event.

    detail carries the full, untruncated tool input/output so callers can
    persist it separately from the short display line (see claude_runner's
    parse_claude_event for the same pattern).
    """
    t = event.get("type")
    if t == "message" and event.get("role") == "assistant":
        text = (event.get("content") or "").strip()
        return (text or None), None
    if t == "function_call":
        name = event.get("name", "")
        args = event.get("arguments", "")
        summary = f"▶ {name}({args[:80]})"
        detail = {"toolType": "tool_use", "name": name, "input": args}
        return summary, detail
    if t == "function_result":
        output = str(event.get("output", ""))
        summary = f"  → {output[:200]}"
        detail = {"toolType": "tool_result", "output": output}
        return summary, detail
    if t == "done":
        return "✓ Done", None
    if t == "error":
        return f"✗ {event.get('message', '')}", None
    return None, None


async def run_codex_auto(
    description: str,
    cwd: str,
    *,
    emit: EmitFn,
    codex_path: str = "codex",
    codex_args: list[str] | None = None,
    openai_api_key: str | None = None,
    model: str | None = None,
) -> tuple[bool, str, str]:
    """Run codex on *description* in *cwd*. Returns (success, stop_reason, last_text).

    *openai_api_key* is injected as OPENAI_API_KEY into the subprocess environment
    when provided, overriding whatever is in the current process env. This lets
    the worker forward the key without requiring it to be set globally before
    import time.

    If *model* is given, passes ``--model <model>`` to select a specific model.
    """
    last_message_file = tempfile.NamedTemporaryFile(
        prefix="pioneer-codex-last-", suffix=".txt", delete=False
    )
    last_message_path = last_message_file.name
    last_message_file.close()
    cmd = [
        codex_path,
        "exec",
        "--json",
        "-C",
        cwd,
        "--output-last-message",
        last_message_path,
        *(["--model", model] if model and "--model" not in (codex_args or []) else []),
        *(codex_args or []),
        description,
    ]
    logger.info("Spawning codex in %s; description=%r", cwd, description)
    logger.info("codex argv: %s", cmd)
    await emit(f"[codex] Starting: {description[:80]}")
    last_text = ""
    stop_reason = "no_events"
    event_count = 0
    master_fd: int | None = None
    slave_fd: int | None = None
    try:
        # `codex exec` treats any non-TTY stdin, including /dev/null, as piped
        # prompt content and tries to drain it. Give it an idle TTY so the
        # explicit prompt argument is the only task input.
        master_fd, slave_fd = pty.openpty()
        env = dict(os.environ)
        if openai_api_key is not None:
            env["OPENAI_API_KEY"] = openai_api_key
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=8 * 1024 * 1024,  # raise readline limit to 8 MB
        )
        os.close(slave_fd)
        slave_fd = None
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
            text, detail = parse_codex_event(event)
            if text:
                await emit(text, detail)
                if not text.startswith(("▶", "✓", "✗", "  →")):
                    last_text = text

        exit_code = await proc.wait()
        await stderr_task
        if event_count == 0:
            logger.warning("codex[%d] produced no stdout events — check PATH/auth", proc.pid)
        if stop_reason == "no_events" and exit_code == 0:
            stop_reason = "success"
        captured_last = Path(last_message_path).read_text(encoding="utf-8").strip()
        if captured_last:
            last_text = captured_last
        return exit_code == 0, stop_reason, last_text
    except FileNotFoundError:
        logger.error("`codex` CLI not found on PATH")
        await emit("[codex] ✗ `codex` CLI not found on PATH")
        return False, "no_events", last_text
    except Exception as exc:  # pragma: no cover
        logger.exception("codex subprocess crashed: %s", exc)
        await emit(f"[codex] ✗ {exc}")
        return False, "error_during_execution", last_text
    finally:
        for fd in (slave_fd, master_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(last_message_path)
