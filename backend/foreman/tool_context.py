"""ToolContext: the collaborator bundle a single ForemanTool handler receives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from foreman.ports import Clock, Events, Github, Scheduler


@dataclass
class ToolContext:
    """Everything a ``ForemanTool`` handler needs beyond its own JSON input.

    Built fresh per tool call by ``_exec_one_tool`` — one ``ToolContext`` per
    invocation, regardless of which of the three legacy context kinds
    (``_CTX_DB``/``_CTX_GITHUB``/``_CTX_NONE``) the tool used to require.

    ``github_token``/``github_username`` are only populated for tools
    registered under ``_CTX_GITHUB``: ``_exec_one_tool`` resolves those
    credentials once per call (via ``_guild_github_token``) before dispatch,
    same as before #1241 — they're carried here rather than added to
    ``Github`` itself because the port is guild-agnostic (a plain GitHub REST
    client), while the token is a per-call, per-guild credential.
    """

    guild_id: str
    user_id: str | None
    db: Any
    guild_pk: int | None
    events: Events
    github: Github
    clock: Clock
    scheduler: Scheduler
    github_token: str | None = None
    github_username: str | None = None
