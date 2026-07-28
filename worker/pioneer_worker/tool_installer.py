"""Dynamic installation of AI coding CLIs (claude, codex, pi) at worker startup.

Worker images no longer bake these in (see the `worker` stage in the root
Dockerfile) so tool versions can be bumped without an image rebuild. Each tool
is installed here the first time it's missing from PATH in a given container;
later calls in the same container are no-ops since npm's global install
directory persists for the container's lifetime.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

logger = logging.getLogger(__name__)

# npm package installed for each tool name via `npm install -g <package>`.
TOOL_PACKAGES: dict[str, str] = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "pi": "@earendil-works/pi-coding-agent",
}

ALL_TOOLS: tuple[str, ...] = tuple(TOOL_PACKAGES)

INSTALL_TIMEOUT_SECONDS = 300.0


def _is_executable(path: str) -> bool:
    """Same PATH/absolute-path resolution as Worker._detect_available_tools."""
    if shutil.which(path):
        return True
    if os.sep in path or (os.altsep and os.altsep in path):
        return os.path.isfile(path) and os.access(path, os.X_OK)
    return False


async def _run_logged(*args: str, timeout: float = INSTALL_TIMEOUT_SECONDS) -> bool:
    """Run a subprocess to completion, logging output. Return True on rc==0."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        logger.error("%s not found — cannot run %r", args[0], list(args))
        return False

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        logger.error("%r timed out after %.0fs", list(args), timeout)
        return False

    output = stdout.decode(errors="replace").strip()
    if proc.returncode == 0:
        logger.info("%r: ok", list(args))
        return True
    logger.error("%r failed (rc=%d)\n%s", list(args), proc.returncode, output)
    return False


async def ensure_tools_installed(tools: list[str], *, tool_paths: dict[str, str]) -> None:
    """Install any of *tools* not already present on PATH.

    *tool_paths* maps tool name -> configured binary path/name (cfg.claude_path
    etc.) so a custom path is honoured the same way detection honours it.
    Unknown tool names are skipped; _detect_available_tools logs its own
    warning for those later.
    """
    for name in tools:
        package = TOOL_PACKAGES.get(name)
        if package is None:
            logger.warning("Unknown tool %r in install list; skipping", name)
            continue
        binary = tool_paths.get(name, name)
        if _is_executable(binary):
            logger.debug("Tool %r already present at %r — skipping install", name, binary)
            continue
        logger.info("Installing %s (%s) via npm...", name, package)
        installed = await _run_logged("npm", "install", "-g", package)
        if installed and name == "pi":
            # Subagent-delegation extension for pi (#938); best-effort, pi
            # itself still works without it.
            await _run_logged("pi", "install", "npm:pi-subagents")
