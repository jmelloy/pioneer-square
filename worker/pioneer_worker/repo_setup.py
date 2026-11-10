"""Prepare repository toolchains before starting a task's coding agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from .git_ops import EmitFn

logger = logging.getLogger(__name__)

MISE_CONFIGS = ("mise.toml", ".mise.toml", ".config/mise/config.toml", ".mise/config.toml")
INSTALL_TIMEOUT = 600


async def install_mise(worktree: str, emit: EmitFn) -> bool:
    """Trust repository configuration, then install its declared tools."""
    configs = [name for name in MISE_CONFIGS if (Path(worktree) / name).is_file()]
    if not configs:
        return True

    await emit(f"Trusting mise configuration and installing tools in {worktree}…", level="worker")
    for command in [("trust", name) for name in configs] + [("install",)]:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "mise",
                *command,
                cwd=worktree,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT)
            for line in output.decode(errors="replace").splitlines():
                await emit(line, level="worker")
            if proc.returncode != 0:
                await emit(f"✗ mise {command[0]} failed (exit {proc.returncode})", level="worker")
                return False
        except (OSError, TimeoutError) as exc:
            reason = (
                f"timed out after {INSTALL_TIMEOUT}s" if isinstance(exc, TimeoutError) else str(exc)
            )
            await emit(f"✗ mise {command[0]}: {reason}", level="worker")
            return False
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
    return True


DEFAULT_SETUP_TIMEOUT = 600
CANCEL_POLL_INTERVAL = 0.25
_MAX_OUTPUT_LINES = 500


@dataclass
class SetupConfig:
    commands: list[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_SETUP_TIMEOUT


@dataclass
class SetupResult:
    ok: bool = True
    cancelled: bool = False
    ran: int = 0


async def fetch_setup_config(
    http_url: str,
    guild_id: str,
    auth_token: str,
    repo: str,
    client: httpx.AsyncClient | None = None,
) -> SetupConfig:
    """Fetch the UI-managed setup commands for *repo*. Never raises: errors mean no setup."""
    url = f"{http_url.rstrip('/')}/guilds/{quote(guild_id, safe='')}/repos/{quote(repo, safe='/')}/setup"
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(url, headers={"Authorization": f"Bearer {auth_token}"})
        if resp.status_code != 200:
            logger.warning("Setup config fetch for %s returned HTTP %s", repo, resp.status_code)
            return SetupConfig()
        body = resp.json()
        commands = [c for c in body.get("setupCommands") or [] if isinstance(c, str) and c.strip()]
        timeout = body.get("timeoutSeconds")
        if not isinstance(timeout, int) or timeout <= 0:
            timeout = DEFAULT_SETUP_TIMEOUT
        return SetupConfig(commands, timeout)
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning("Setup config fetch for %s failed: %s", repo, exc)
        return SetupConfig()
    finally:
        if own:
            await client.aclose()


async def toolchain_env(worktree: str, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment with mise-managed tools on PATH for *worktree*.

    Returns only the variables mise adds/changes (``mise env --json``); empty when
    the repo has no mise config or mise is unavailable.
    """
    if not any((Path(worktree) / name).is_file() for name in MISE_CONFIGS):
        return {}
    env = {**os.environ, **(base_env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            "mise",
            "env",
            "--json",
            cwd=worktree,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return {}
        data = json.loads(out.decode())
    except (OSError, TimeoutError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()


async def run_setup_commands(
    worktree: str,
    config: SetupConfig,
    emit: EmitFn,
    env: dict[str, str] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> SetupResult:
    """Run each setup command in *worktree*, streaming output to *emit*.

    *env* is layered over the process environment (toolchain + task vars).
    Stops at the first failure or timeout. The timeout applies per command.
    Polls *is_cancelled* and kills the command's process group when it flips.
    """
    result = SetupResult()
    full_env = {**os.environ, **(env or {})}
    for command in config.commands:
        if is_cancelled and is_cancelled():
            result.cancelled = True
            return result
        await emit(f"$ {command}", level="worker")
        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=worktree,
                env=full_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            lines_seen = 0
            truncated = False
            deadline = asyncio.get_running_loop().time() + config.timeout_seconds

            async def _drain(p=proc) -> None:
                nonlocal lines_seen, truncated
                assert p.stdout is not None
                async for raw in p.stdout:
                    lines_seen += 1
                    if lines_seen <= _MAX_OUTPUT_LINES:
                        await emit(raw.decode(errors="replace").rstrip("\n"), level="worker")
                    elif not truncated:
                        truncated = True
                        await emit("… further output truncated …", level="worker")

            drain = asyncio.ensure_future(_drain())
            reason = None
            while not drain.done():
                if is_cancelled and is_cancelled():
                    reason = "cancelled"
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    reason = "timeout"
                    break
                await asyncio.wait({drain}, timeout=CANCEL_POLL_INTERVAL)
            if reason:
                await _kill_group(proc)
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await drain
                if reason == "cancelled":
                    await emit(f"✗ setup cancelled: {command}", level="worker")
                    result.cancelled = True
                else:
                    await emit(
                        f"✗ setup timed out after {config.timeout_seconds}s: {command}",
                        level="worker",
                    )
                    result.ok = False
                return result
            await drain
            await proc.wait()
            if proc.returncode != 0:
                await emit(f"✗ setup failed (exit {proc.returncode}): {command}", level="worker")
                result.ok = False
                return result
            result.ran += 1
        except OSError as exc:
            await emit(f"✗ setup could not run: {command}: {exc}", level="worker")
            result.ok = False
            return result
        finally:
            if proc is not None:
                await _kill_group(proc)
    if config.commands:
        await emit(f"✓ setup complete ({result.ran} command(s))", level="worker")
    return result
