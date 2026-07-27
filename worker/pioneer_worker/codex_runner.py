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

    Codex ``exec --json`` emits a threaded event schema (codex-cli >= ~0.30):
    ``thread.started`` / ``turn.started`` / ``turn.completed`` envelopes plus
    ``item.started`` / ``item.completed`` events carrying a nested ``item``
    with its own ``type`` (``agent_message``, ``command_execution``, etc.).

    detail carries the full, untruncated tool input/output so callers can
    persist it separately from the short display line (see claude_runner's
    parse_claude_event for the same pattern).
    """
    t = event.get("type")

    if t == "turn.completed":
        return "✓ Done", None
    if t == "error":
        return f"✗ {event.get('message', '')}", None

    if t in ("item.started", "item.completed"):
        item = event.get("item") or {}
        it = item.get("type")
        completed = t == "item.completed"

        if it == "agent_message":
            if not completed:
                return None, None  # wait for the final text on item.completed
            text = (item.get("text") or "").strip()
            return (text or None), None

        if it == "reasoning":
            if not completed:
                return None, None
            text = (item.get("text") or "").strip()
            return (f"💭 {text}" if text else None), None

        if it == "command_execution":
            cmd = item.get("command", "")
            if not completed:
                return f"▶ {cmd[:100]}", {"toolType": "tool_use", "name": "shell", "input": cmd}
            output = str(item.get("aggregated_output", ""))
            exit_code = item.get("exit_code")
            summary = f"  → (exit {exit_code}) {output[:200]}"
            return summary, {"toolType": "tool_result", "output": output}

        # Any other item type (mcp_tool_call, file_change, web_search, todo_list…):
        # surface it once on completion with a tool-like prefix so it never gets
        # mistaken for the agent's final message.
        if completed and it:
            detail = {"toolType": "tool_use", "name": it, "input": json.dumps(item)}
            return f"▶ {it}", detail
        return None, None

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
    resume_session_id: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, str, str | None]:
    """Run codex on *description* in *cwd*. Returns (success, stop_reason, last_text, session_id).

    *openai_api_key* is injected as OPENAI_API_KEY into the subprocess environment
    when provided, overriding whatever is in the current process env. This lets
    the worker forward the key without requiring it to be set globally before
    import time.

    If *model* is given, passes ``--model <model>`` to select a specific model.

    If *resume_session_id* is given, runs ``codex exec resume <id>`` so codex
    continues the previous thread with full context. Codex exits non-zero when
    the session no longer exists on this machine (e.g. a different worker ran
    the prior turn); in that case this falls back to a fresh session silently
    and retries once.
    """
    success, stop_reason, last_text, session_id = await _run_codex_once(
        description,
        cwd,
        emit=emit,
        codex_path=codex_path,
        codex_args=codex_args,
        openai_api_key=openai_api_key,
        model=model,
        resume_session_id=resume_session_id,
        env=env,
    )
    if resume_session_id and not success:
        logger.warning(
            "codex resume of session %s failed (stop_reason=%s) — falling back to a fresh session",
            resume_session_id,
            stop_reason,
        )
        await emit("[codex] Resume failed — starting a fresh session.")
        success, stop_reason, last_text, session_id = await _run_codex_once(
            description,
            cwd,
            emit=emit,
            codex_path=codex_path,
            codex_args=codex_args,
            openai_api_key=openai_api_key,
            model=model,
            resume_session_id=None,
            env=env,
        )
    return success, stop_reason, last_text, session_id


async def _run_codex_once(
    description: str,
    cwd: str,
    *,
    emit: EmitFn,
    codex_path: str,
    codex_args: list[str] | None,
    openai_api_key: str | None,
    model: str | None,
    resume_session_id: str | None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, str, str | None]:
    """Single codex invocation. See run_codex_auto for the retrying wrapper."""
    last_message_file = tempfile.NamedTemporaryFile(
        prefix="pioneer-codex-last-", suffix=".txt", delete=False
    )
    last_message_path = last_message_file.name
    last_message_file.close()
    cmd = [
        codex_path,
        "exec",
        "-C",
        cwd,
        *(["resume", resume_session_id] if resume_session_id else []),
        "--json",
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
    session_id = resume_session_id
    master_fd: int | None = None
    slave_fd: int | None = None
    try:
        # `codex exec` treats any non-TTY stdin, including /dev/null, as piped
        # prompt content and tries to drain it. Give it an idle TTY so the
        # explicit prompt argument is the only task input.
        master_fd, slave_fd = pty.openpty()
        subprocess_env = dict(env) if env is not None else dict(os.environ)
        if openai_api_key is not None:
            subprocess_env["OPENAI_API_KEY"] = openai_api_key
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env,
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
            etype = event.get("type")
            if etype == "thread.started":
                tid = event.get("thread_id")
                if tid:
                    session_id = tid
                    logger.info("codex[%d] thread_id=%s", proc.pid, session_id)
            if etype == "turn.completed":
                stop_reason = "success"
            elif etype == "error" or (
                etype in ("item.started", "item.completed")
                and (event.get("item") or {}).get("type") == "error"
            ):
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
        return exit_code == 0, stop_reason, last_text, session_id
    except FileNotFoundError:
        logger.error("`codex` CLI not found on PATH")
        await emit("[codex] ✗ `codex` CLI not found on PATH")
        return False, "no_events", last_text, session_id
    except Exception as exc:  # pragma: no cover
        logger.exception("codex subprocess crashed: %s", exc)
        await emit(f"[codex] ✗ {exc}")
        return False, "error_during_execution", last_text, session_id
    finally:
        for fd in (slave_fd, master_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(last_message_path)
