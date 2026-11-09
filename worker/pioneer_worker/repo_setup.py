"""Prepare repository toolchains before starting a task's coding agent."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from .git_ops import EmitFn

MISE_CONFIGS = ("mise.toml", ".mise.toml", ".config/mise/config.toml", ".mise/config.toml")
INSTALL_TIMEOUT = 600


async def install_mise(worktree: str, emit: EmitFn) -> bool:
    """Install declared tools without granting trust to repository configuration."""
    if not any((Path(worktree) / name).is_file() for name in MISE_CONFIGS):
        return True

    await emit(f"Installing mise tools in {worktree}…", level="worker")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "mise",
            "install",
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
            await emit(f"✗ mise install failed (exit {proc.returncode})", level="worker")
            return False
        return True
    except (OSError, TimeoutError) as exc:
        reason = (
            f"timed out after {INSTALL_TIMEOUT}s" if isinstance(exc, TimeoutError) else str(exc)
        )
        await emit(f"✗ mise install: {reason}", level="worker")
        return False
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
