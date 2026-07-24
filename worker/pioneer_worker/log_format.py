"""Helpers for formatting file paths in worker activity logs."""

from __future__ import annotations

import re

# Task worktrees live at .../<guild_id>/<worker_id>/t-<taskid>/<repo_name>/...
# Strip everything up to and including the task worktree segment so logs show
# only the repo-relative path, not the host's absolute filesystem layout.
_TASK_WORKTREE_RE = re.compile(r"/t-[a-z0-9]+/")


def strip_worktree_prefix(path: str) -> str:
    """Strip the host filesystem prefix from a task worktree path.

    Given ".../w-4de676/t-vdfpj3/dnsid-infra/envs/dev/main.tf", returns
    "dnsid-infra/envs/dev/main.tf". Paths without a task worktree marker
    (e.g. relative paths, or paths outside any task worktree) are returned
    unchanged.
    """
    if not path:
        return path
    matches = list(_TASK_WORKTREE_RE.finditer(path))
    if not matches:
        return path
    return path[matches[-1].end() :]
