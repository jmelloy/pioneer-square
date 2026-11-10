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
