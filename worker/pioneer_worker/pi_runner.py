"""Run `pi --mode rpc` on a task and stream output."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable

from .log_format import strip_worktree_prefix
from .runner_types import RunRequest, RunResult, StopReason  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# Pioneer names its Bedrock provider "bedrock" everywhere (foreman config,
# guild pi_default_provider, task.provider), but pi's CLI calls it
# "amazon-bedrock" — passing "bedrock" straight through makes pi exit 1 with
# `Unknown provider "bedrock"`. Translate at the pi boundary only.
_PI_PROVIDER_ALIASES = {"bedrock": "amazon-bedrock"}


def pi_provider_arg(provider: str | None) -> str | None:
    """Map a pioneer provider id to pi's CLI provider name (pass-through if unmapped)."""
    return _PI_PROVIDER_ALIASES.get(provider, provider) if provider else provider


EmitFn = Callable[..., Awaitable[None]]  # emit(line: str, detail: dict | None = None)
UsageFn = Callable[[dict], Awaitable[None]]  # on_usage(record: dict)
OnProcFn = Callable[["PiProcess"], None]  # on_proc(proc) — worker's live-handle callback


def parse_pi_model_rows(output: str) -> list[dict]:
    """Parse `pi --list-models` tabular output into model records.

    The command has no JSON mode today; it prints whitespace-separated columns:
    provider model context max-out thinking images. Provider/model ids do not
    contain spaces, so split() is sufficient and keeps this tolerant of column
    width changes.
    """
    rows: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("provider", "Warning")):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        provider, model, context, max_out, thinking, images = parts[:6]
        rows.append(
            {
                "provider": provider,
                "id": model,
                "name": model,
                "context": context,
                "maxOutput": max_out,
                "thinking": thinking.lower() == "yes",
                "images": images.lower() == "yes",
            }
        )
    return rows


async def list_pi_models(
    *,
    pi_path: str = "pi",
    env: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> list[dict]:
    """Return models visible to the current pi environment.

    Pi filters this list to providers with usable local credentials/config, so
    callers should run it inside the worker environment they intend to use for
    actual pi tasks.
    """
    proc = await asyncio.create_subprocess_exec(
        pi_path,
        "--list-models",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        logger.warning("pi --list-models timed out after %ss; killing pid=%s", timeout, proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
            await proc.wait()
        return []
    if proc.returncode != 0:
        logger.warning("pi --list-models rc=%d", proc.returncode)
        return []
    return parse_pi_model_rows(stdout.decode(errors="replace"))


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """Signal pi's whole process group, not just pi's own pid.

    pi is spawned with ``start_new_session=True`` so it leads its own process
    group; signalling the group also reaps the tool subprocesses pi spawns
    (bash, the model client). A bare ``proc.kill()`` orphans those onto the
    container's PID 1, which doesn't reap them.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except Exception as exc:
        if isinstance(exc, ProcessLookupError):
            return
        logger.debug("pi killpg failed", exc_info=True)


class PiProcess:
    """Live handle the worker holds so it can cancel/redirect a running pi.

    Duck-compatible with claude_runner.ClaudeProcess (``terminate`` /
    ``send_message`` / ``session_id``) so worker.py's cancel, redirect, and
    message-injection paths work for pi too. Unlike ClaudeProcess, send_message
    frames the text as a pi RPC ``prompt`` event and terminate signals the
    whole process group.
    """

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.session_id: str | None = None

    async def send_message(self, text: str) -> bool:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            return False
        try:
            msg = json.dumps({"type": "prompt", "message": text}) + "\n"
            self.proc.stdin.write(msg.encode())
            await self.proc.stdin.drain()
            return True
        except Exception:
            return False

    async def terminate(self) -> None:
        _signal_group(self.proc, signal.SIGTERM)


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

    Pi has emitted several shapes over time:
    - camelCase: inputTokens/outputTokens/cacheReadInputTokens/cacheCreationInputTokens
    - snake_case: input_tokens/output_tokens/cache_read_input_tokens/cache_creation_input_tokens
    - current RPC: input/output/cacheRead/cacheWrite plus cost nested under usage.cost
    Returns None when no usage block is found.
    """
    usage = event.get("usage") or event.get("tokenUsage")
    if not isinstance(usage, dict):
        return None
    v = usage.get("inputTokens")
    input_tokens = v if v is not None else usage.get("input_tokens", usage.get("input", 0))
    v = usage.get("outputTokens")
    output_tokens = v if v is not None else usage.get("output_tokens", usage.get("output", 0))
    v = usage.get("cacheReadInputTokens")
    cache_read = (
        v if v is not None else usage.get("cache_read_input_tokens", usage.get("cacheRead", 0))
    )
    v = usage.get("cacheCreationInputTokens")
    cache_creation = (
        v if v is not None else usage.get("cache_creation_input_tokens", usage.get("cacheWrite", 0))
    )
    try:
        return {
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cache_read_input_tokens": int(cache_read),
            "cache_creation_input_tokens": int(cache_creation),
        }
    except (TypeError, ValueError):
        return None


def _event_model(event: dict) -> str | None:
    model = event.get("model")
    if model:
        return model
    msg = event.get("message")
    if isinstance(msg, dict):
        return msg.get("model")
    messages = event.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("model"):
                return msg.get("model")
    return None


def _event_cost_usd(event: dict):
    cost = event.get("cost_usd") or event.get("costUsd")
    if cost is not None:
        return cost
    usage = event.get("usage")
    if isinstance(usage, dict):
        nested = usage.get("cost")
        if isinstance(nested, dict) and nested.get("total") is not None:
            return nested.get("total")
    messages = event.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict):
                cost = _event_cost_usd(msg)
                if cost is not None:
                    return cost
    return None


def parse_pi_event(event: dict, last_text: str) -> tuple[str | None, dict | None, str]:
    """Extract a human-readable line (and full-content detail) from one pi RPC event.

    Returns (display_text_or_None, detail_or_None, updated_last_text). detail
    carries the full, untruncated tool input/output so callers can persist it
    separately from the short display line (see claude_runner's
    parse_claude_event for the same pattern).
    """
    t = event.get("type")
    if t == "message_update":
        # Current pi RPC streams assistant deltas under assistantMessageEvent;
        # older versions repeated the full assistant message under message.content.
        ame = event.get("assistantMessageEvent", {})
        if ame.get("type") == "text_delta":
            delta = ame.get("delta", "")
            return (delta if delta.strip() else None), None, last_text + delta
        if ame.get("type") == "text_end":
            full = ame.get("content", "")
            delta = full[len(last_text) :]
            return (delta if delta.strip() else None), None, full
        full = ""
        for blk in event.get("message", {}).get("content", []):
            if isinstance(blk, dict) and blk.get("type") == "text":
                full += blk.get("text", "")
        if full:
            delta = full[len(last_text) :]
            return (delta if delta.strip() else None), None, full
        return None, None, last_text
    if t == "tool_execution_start":
        name = event.get("toolName", "")
        inp = event.get("args", {})
        if name == "bash":
            summary = f"▶ bash: {inp.get('command', '')[:120]}"
        elif name in ("read", "write", "edit"):
            fp = strip_worktree_prefix(inp.get("path", inp.get("file_path", "")))
            summary = f"▶ {name}: {fp}"
        else:
            summary = f"▶ {name}({json.dumps(inp)[:80]})"
        detail = {"toolType": "tool_use", "name": name, "input": inp}
        return summary, detail, last_text
    if t == "tool_execution_end":
        out = _result_text(event.get("result", {})).strip()
        if not out:
            return None, None, last_text
        lines = out.split("\n")
        preview = lines[0][:120]
        if len(lines) > 1:
            preview += f" (+{len(lines) - 1} lines)"
        prefix = "  ✗ " if event.get("isError") else "  → "
        detail = {"toolType": "tool_result", "output": out}
        return f"{prefix}{preview}", detail, last_text
    if t == "message_end" and event.get("message", {}).get("role") == "assistant":
        # If a provider/version only emits the finalized assistant message,
        # still capture and display the text. When text_delta already streamed,
        # delta will be empty and nothing is re-emitted.
        full = ""
        for blk in event.get("message", {}).get("content", []):
            if isinstance(blk, dict) and blk.get("type") == "text":
                full += blk.get("text", "")
        if full:
            delta = full[len(last_text) :]
            return (delta if delta.strip() else None), None, full
    if t == "agent_start":
        return "[pi] agent started", None, last_text
    return None, None, last_text


async def run_pi_auto(
    description: str,
    cwd: str,
    *,
    emit: EmitFn,
    on_usage: UsageFn | None = None,
    on_proc: OnProcFn | None = None,
    pi_path: str = "pi",
    model: str | None = None,
    provider: str | None = None,
    resume_session_id: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str, str, str | None]:
    """Run pi on *description* in *cwd*.

    Returns (success, stop_reason, last_text, session_id).

    Session tracking is enabled by default (no ``--no-session`` flag).
    Pi emits a ``session_id`` field in ``agent_start`` (or a dedicated
    ``session`` event); the value is extracted and returned so callers can
    resume or reference the session.

    If *resume_session_id* is given, passes ``--session <id>`` so pi continues
    the previous session with full context. Pi exits non-zero when the session
    no longer exists on this machine (e.g. a different worker ran the prior
    turn); in that case this falls back to a fresh session silently and
    retries once.

    If *on_usage* is provided it is called with usage-record dicts following
    the same shape used by claude_runner:
      - ``{"kind": "api_call", "model": ..., "input_tokens": ..., ...}`` for
        per-message token counts emitted in ``message_update`` events.
      - ``{"kind": "result", "model": ..., "cost_usd": ..., ...}`` for the
        final summary emitted in ``agent_end``.
    """
    success, stop_reason, last_text, session_id = await _run_pi_once(
        description,
        cwd,
        emit=emit,
        on_usage=on_usage,
        on_proc=on_proc,
        pi_path=pi_path,
        model=model,
        provider=provider,
        resume_session_id=resume_session_id,
        env=env,
        interactive=False,
    )
    if resume_session_id and not success:
        logger.warning(
            "pi resume of session %s failed (stop_reason=%s) — falling back to a fresh session",
            resume_session_id,
            stop_reason,
        )
        await emit("[pi] Resume failed — starting a fresh session.")
        success, stop_reason, last_text, session_id = await _run_pi_once(
            description,
            cwd,
            emit=emit,
            on_usage=on_usage,
            on_proc=on_proc,
            pi_path=pi_path,
            model=model,
            provider=provider,
            resume_session_id=None,
            env=env,
            interactive=False,
        )
    return success, stop_reason, last_text, session_id


async def _run_pi_once(
    description: str,
    cwd: str,
    *,
    emit: EmitFn,
    on_usage: UsageFn | None,
    on_proc: OnProcFn | None,
    pi_path: str,
    model: str | None,
    provider: str | None,
    resume_session_id: str | None,
    env: dict[str, str] | None = None,
    interactive: bool = False,
) -> tuple[bool, str, str, str | None]:
    """Single pi invocation. See run_pi_auto for the retrying wrapper."""
    cmd = [pi_path]
    if resume_session_id:
        cmd += ["--session", resume_session_id]
    cmd += ["--mode", "rpc"]
    if provider:
        # `--provider` only takes effect alongside `--model`; on its own pi
        # stays on its default provider. `--models {provider}/*` actually
        # switches the active provider (and picks its first matching model),
        # which is what we want when a provider is set without a specific model.
        cmd += ["--models", f"{pi_provider_arg(provider)}/*"]
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
    session_id: str | None = resume_session_id
    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[None] | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
            env=env,
            # Own process group so _signal_group() can reap pi's tool children.
            start_new_session=True,
        )
        logger.info("pi subprocess started pid=%s", proc.pid)
        # Hand the worker a live handle so its cancel/redirect/message paths
        # (which look at Agent.current_claude) can reach this pi process.
        if on_proc is not None:
            on_proc(PiProcess(proc))

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

        async def _read_stdout_line() -> bytes | asyncio.LimitOverrunError:
            try:
                return await proc.stdout.readline()  # type: ignore[union-attr]
            except Exception as exc:
                if isinstance(exc, asyncio.LimitOverrunError):
                    return exc
                raise

        while True:
            raw_or_exc = await _read_stdout_line()
            if isinstance(raw_or_exc, asyncio.LimitOverrunError):
                logger.error(
                    "pi[%d] stdout line exceeded StreamReader limit, skipping line: %s",
                    proc.pid,
                    raw_or_exc,
                )
                await emit(f"[pi] ✗ stdout line too large, skipping: {raw_or_exc}")
                # Drain the rest of the oversized line one byte at a time so we
                # stop exactly at the \n and don't consume bytes from the next line.
                with contextlib.suppress(Exception):
                    while True:
                        byte = await proc.stdout.read(1)  # type: ignore[union-attr]
                        if not byte or byte == b"\n":
                            break
                continue
            raw = raw_or_exc
            if not raw:  # EOF
                break
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
                    usage_event = event
                    if not _parse_pi_usage(usage_event):
                        messages = event.get("messages")
                        if isinstance(messages, list):
                            for msg in reversed(messages):
                                if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                                    usage_event = msg
                                    break
                    usage_data = _parse_pi_usage(usage_event)
                    cost = _event_cost_usd(event)
                    if usage_data or cost is not None:
                        rec: dict = {
                            "kind": "result",
                            "model": _event_model(event),
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
                                "model": _event_model(event),
                                **usage_data,
                            }
                        )
            if etype in ("message_start", "message_end", "turn_end"):
                msg = event.get("message")
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    error_msg = msg.get("errorMessage")
                    if msg.get("stopReason") == "error" or error_msg:
                        saw_error = True
                        await _flush_text_buf()
                        await emit(f"[pi] ✗ {error_msg or 'assistant message failed'}")
            text, detail, accumulated = parse_pi_event(event, accumulated)
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
                    await emit(text, detail)
            if etype == "message_update" and accumulated.strip():
                last_text = accumulated
            # We only send a single prompt; pi RPC mode stays alive waiting
            # for more stdin after the run completes, so stop reading once the
            # agent (or a fatal error) has finished.
            if saw_error or (saw_agent_end and not interactive):
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
            _signal_group(proc, signal.SIGKILL)
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
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            _signal_group(proc, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except BaseException:
                logger.debug("pi wait-after-kill failed", exc_info=True)
        # Cancel the stderr drain task if it is still running.
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task


_STOP_REASON_MAP: dict[str, StopReason] = {
    "success": StopReason.SUCCESS,
    "error_during_execution": StopReason.ERROR,
    "no_events": StopReason.NO_EVENTS,
}


def _map_stop_reason(raw: str) -> StopReason:
    try:
        return _STOP_REASON_MAP[raw]
    except KeyError:
        raise ValueError(f"pi runner produced unknown stop_reason {raw!r}") from None


class PiRunner:
    """Pi adapter for the shared Runner seam."""

    # pi resolves credentials per provider, so there is no single env var to name.
    credential_hint = "a provider credential pi recognises"

    def __init__(
        self,
        *,
        pi_path: str = "pi",
        model: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.pi_path = pi_path
        self.model = model
        self.provider = provider

    @property
    def binary_path(self) -> str:
        return self.pi_path

    async def run(self, req: RunRequest) -> RunResult:
        success, stop_reason, last_text, session_id = await run_pi_auto(
            req.description,
            req.cwd,
            emit=req.emit,
            on_usage=req.on_usage,
            on_proc=req.on_proc,
            pi_path=self.pi_path,
            model=req.model or self.model,
            provider=req.provider or self.provider,
            resume_session_id=req.resume_session_id,
            env=req.env,
        )
        return RunResult(
            success=success,
            stop_reason=_map_stop_reason(stop_reason),
            final_message=last_text,
            session_id=session_id,
            raw_stop_reason=stop_reason,
        )

    async def probe_credentials(self, env: dict[str, str]) -> bool:
        return bool(await self.list_models(env))

    async def list_models(self, env: dict[str, str]) -> list[dict]:
        """Live pi catalog, or [] when the binary is missing or won't spawn.

        Tool detection calls this before every credential probe, so a spawn
        failure has to read as "no models" rather than propagate and abort the
        whole detection pass.
        """
        try:
            return await list_pi_models(pi_path=self.pi_path, env=env, timeout=20.0)
        except FileNotFoundError as exc:
            logger.warning("pi binary not found at %r: %s", self.pi_path, exc)
            return []
        except Exception as exc:
            logger.warning("pi --list-models spawn failed: %s", exc)
            return []
