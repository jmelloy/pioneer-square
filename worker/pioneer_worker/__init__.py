"""Pioneer Square worker agent.

Standalone process that connects to a Pioneer Square backend over WebSocket,
listens for task assignments, clones the configured repos into git worktrees,
runs `claude --dangerously-skip-permissions` on each task, and pushes the
result as a GitHub pull request.
"""

__version__ = "0.1.0"
