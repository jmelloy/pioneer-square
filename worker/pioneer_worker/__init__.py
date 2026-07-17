"""Pioneer Square worker agent.

Standalone process that connects to a Pioneer Square backend over WebSocket,
listens for task assignments, clones the configured repos into git worktrees,
runs `claude --dangerously-skip-permissions` on each task, and pushes the
result as a GitHub pull request.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pioneer-square")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"
