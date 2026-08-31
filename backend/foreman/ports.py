"""Protocols for ForemanRun's collaborators and ToolContext's ports.

Two distinct seams share this module (see issue #1241):

  - ``ForemanRun(llm, tools, journal, history)`` — the round loop's own four
    collaborators (``LLM`` here; ``Journal``/``History`` live in
    ``foreman.journal``/``foreman.history`` since they're concrete classes
    with only one real implementation each, not swappable backends).
  - ``ToolContext(events, github, clock, scheduler)`` — the ports individual
    ``ForemanTool`` handlers need. ``Events`` is shared by both: both
    ``TurnJournal`` (WS narration) and tool handlers (broadcasting their own
    domain events, e.g. task-created) need it.

Concrete production adapters (``RealEvents``, ``RealGithub``, ``RealScheduler``,
``SystemClock``) are defined here too, but deliberately delegate to the
module-level names already owned by ``foreman.tools`` (``broadcast``,
``broadcast_msg``, ``emit_terminal_line``, ``spawn``, ``_gh_api``) via a
deferred import instead of capturing their own reference to ``events``/
``util.tasks``. That's what keeps the ~180 existing tests that
``patch("foreman.tools.broadcast", ...)`` (etc.) working unchanged: the patch
target is still the attribute the adapters look up at call time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


class Events(Protocol):
    """WS broadcast + terminal-line narration."""

    async def broadcast(self, guild_id: str, message: dict) -> None: ...

    async def broadcast_msg(self, guild_id: str, message: Any) -> None: ...

    async def emit_terminal_line(self, guild_id: str, agent_id: str, line: str) -> None: ...


class Github(Protocol):
    """Read access to the GitHub REST API."""

    async def get(self, path: str, *, token: str | None = None, timeout: float = 15.0) -> Any: ...


class Clock(Protocol):
    """Injectable wall-clock, so time-sensitive handlers/tests can freeze it."""

    def now(self) -> datetime: ...


class Scheduler(Protocol):
    """Fire-and-forget background task scheduling (wraps ``util.tasks.spawn``)."""

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task: ...


@dataclass
class LLMResult:
    """One LLM turn: its content blocks plus the api_request_log row produced.

    ``content`` holds Anthropic-SDK-shaped content blocks (``.type``/``.text``/
    ``.input``/``.id``/``.name`` attribute access) — the same shape
    ``foreman.llm.call_anthropic`` and the proxy's ``_ProxyResponse`` already
    produce, so ``ForemanRun`` doesn't need to know which path served it.
    """

    content: list[Any]
    stop_reason: str | None
    api_log_id: int | None


class LLM(Protocol):
    """One resolved (provider, model, client) able to make Foreman LLM calls.

    A single ``LLM`` instance is constructed once per ``ForemanRun`` (see
    ``foreman.runner.RealLLM``) — it already knows the guild/task/user/trigger
    metadata needed for ``api_request_log`` rows, so ``ForemanRun.execute``
    doesn't have to thread that through every call.
    """

    async def call(
        self,
        *,
        system_blocks: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> LLMResult: ...


class SystemClock:
    """Production ``Clock``: wall-clock UTC time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class RealEvents:
    """Production ``Events``, delegating to ``foreman.tools``'s own globals.

    Deferred import so this stays patchable via
    ``patch("foreman.tools.broadcast", ...)`` / ``.broadcast_msg`` /
    ``.emit_terminal_line`` exactly like the pre-#1241 direct calls were.
    """

    async def broadcast(self, guild_id: str, message: dict) -> None:
        from foreman import tools as _tools

        await _tools.broadcast(guild_id, message)

    async def broadcast_msg(self, guild_id: str, message: Any) -> None:
        from foreman import tools as _tools

        await _tools.broadcast_msg(guild_id, message)

    async def emit_terminal_line(self, guild_id: str, agent_id: str, line: str) -> None:
        from foreman import tools as _tools

        await _tools.emit_terminal_line(guild_id, agent_id, line)


class RealGithub:
    """Production ``Github``, delegating to ``foreman.tools._gh_api``.

    Only GETs are ported to this port in #1241 — the write endpoints
    (``_gh_api_post``, ``_create_pr_api``, ``_gh_graphql``) stay direct calls;
    see the issue for why that's an intentionally narrower scope.
    """

    async def get(self, path: str, *, token: str | None = None, timeout: float = 15.0) -> Any:
        from foreman import tools as _tools

        return await _tools._to_thread(_tools._gh_api, path, token)


class RealScheduler:
    """Production ``Scheduler``, delegating to ``foreman.tools.spawn``."""

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
        from foreman import tools as _tools

        return _tools.spawn(coro, name=name)
