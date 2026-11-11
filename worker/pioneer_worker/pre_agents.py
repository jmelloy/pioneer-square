"""Resolve and apply the optional "pre-AGENTS.md" prompt.

The prompt is prepended to the task description so the agent sees it before it
reads the repository's AGENTS.md. Precedence (highest first):

1. ``pre_agents_prompt`` in the task payload
2. ``pre_agents_prompt`` in ``pioneer-worker.toml``
3. ``PIONEER_PRE_AGENTS_PROMPT`` environment variable
4. Guild-level default, delivered as ``guild_pre_agents_prompt`` in the task

A source that is unset or whitespace-only is skipped. An explicitly empty
string in the task payload (``""``) disables injection for that task.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

ENV_VAR = "PIONEER_PRE_AGENTS_PROMPT"
TASK_KEY = "pre_agents_prompt"
GUILD_KEY = "guild_pre_agents_prompt"


def resolve_prompt(
    task: Mapping[str, Any] | None,
    toml_value: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the effective prompt (stripped), or "" if none applies."""
    if task is not None:
        task_val = task.get(TASK_KEY)
        if isinstance(task_val, str):
            # Present string wins, even if empty (explicit opt-out).
            return task_val.strip()
    if isinstance(toml_value, str) and toml_value.strip():
        return toml_value.strip()
    env = os.environ if environ is None else environ
    env_val = (env.get(ENV_VAR) or "").strip()
    if env_val:
        return env_val
    if task is not None:
        guild_val = task.get(GUILD_KEY)
        if isinstance(guild_val, str):
            return guild_val.strip()
    return ""


def apply_pre_agents(
    description: str,
    task: Mapping[str, Any] | None,
    toml_value: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Prepend the effective prompt to ``description`` (no-op if none)."""
    prompt = resolve_prompt(task, toml_value, environ)
    if not prompt:
        return description
    return f"{prompt}\n\n{description}"
