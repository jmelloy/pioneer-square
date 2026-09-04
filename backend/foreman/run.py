"""ForemanRun: the Foreman round loop, extracted from ``foreman.runner._run_foreman_ai``.

Takes its four collaborators explicitly instead of reaching for module
globals — ``LLM``, ``ToolExecutor``, ``Journal``, ``History`` — so a turn can
be driven with a scripted LLM and an in-memory journal in tests, with no
``patch()`` of private functions (see ``foreman.journal``/``foreman.history``
for the production implementations of ``Journal``/``History``).

What's deliberately *not* here: prompt/context assembly (workers query, tasks
query, thread resolution) stays in ``foreman.runner`` as ordinary DB reads —
``ForemanRun.execute`` receives the rendered ``system_blocks``/
``state_preamble``/``audit_system`` as arguments rather than building them
itself, so this module has no DB dependency of its own.

Issue #1271: History loading now prefers ``thread_id`` when available via
``HistoryByThread`` protocol, falling back to the legacy (guild_id, user_id)
pair for compatibility.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from foreman.github_url_parser import annotate_message as _annotate_github_urls
from foreman.history import History, HistoryByThread
from foreman.journal import Journal
from foreman.message_utils import (
    _inject_state_preamble,
    _serialize_content,
    _stamp_message_cache_breakpoint,
    prune_history,
    strip_orphaned_tool_results,
    truncate_tool_result,
)
from foreman.ports import LLM
from foreman.tools_schema import FOREMAN_TOOLS

logger = logging.getLogger(__name__)


class ToolExecutor(Protocol):
    async def exec(
        self, guild_id: str, tool_uses: list[Any], *, user_id: str | None = None
    ) -> list[dict]: ...


class RealToolExecutor:
    """Production ``ToolExecutor``, delegating to ``foreman.tools.exec_tools``.

    Deferred import so tests that ``patch("foreman.tools.exec_tools", ...)``
    (or patch the handlers/ports it dispatches to) keep working unchanged.
    """

    async def exec(
        self, guild_id: str, tool_uses: list[Any], *, user_id: str | None = None
    ) -> list[dict]:
        from foreman import tools as _tools

        return await _tools.exec_tools(guild_id, tool_uses, user_id=user_id)


@dataclass
class RunConfig:
    guild_id: str
    user_id: str
    task_id: str | None
    trigger: str | None
    max_rounds: int
    # Thread the foreman is operating in (#1271). When set, enables
    # thread-scoped history retrieval without (guild_id, user_id) pair lookup.
    thread_id: str | None = None


class ForemanRun:
    """One Foreman turn: system+human -> round loop -> (forced wrap-up if capped)."""

    def __init__(
        self,
        cfg: RunConfig,
        *,
        llm: LLM,
        tools: ToolExecutor,
        journal: Journal,
        history: History | HistoryByThread,
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tools = tools
        self._journal = journal
        self._history = history

    async def execute(
        self,
        human_message: str,
        *,
        system_blocks: list[dict[str, Any]],
        state_preamble: str,
        audit_system: str,
    ) -> None:
        human_message = _annotate_github_urls(human_message)

        # Persist the rendered prompt + human turn for auditing; the API
        # receives system_blocks (cacheable) and the state preamble injected
        # at send time below — the DB still holds just the human's literal text.
        await self._journal.system(audit_system)
        await self._journal.human(human_message)

        # Prefer thread-scoped history when thread_id is available (#1271),
        # falling back to (guild_id, user_id) pair for legacy compatibility.
        if self._cfg.thread_id and hasattr(self._history, "load_for_llm_by_thread"):
            messages = await self._history.load_for_llm_by_thread(self._cfg.thread_id)
        else:
            messages = await self._history.load_for_llm(self._cfg.guild_id, self._cfg.user_id)
        _inject_state_preamble(messages, state_preamble)

        capped = True
        for round_num in range(self._cfg.max_rounds):
            messages, tool_uses = await self._one_round(messages, system_blocks, round_num)
            if not tool_uses:
                capped = False
                break

        if capped:
            logger.warning(
                "guild=%s ForemanRun: hit %d-round safety cap, forcing wrap-up",
                self._cfg.guild_id,
                self._cfg.max_rounds,
            )
            await self._wrap_up(messages, system_blocks)

    async def _wrap_up(self, messages: list[dict], system_blocks: list[dict]) -> None:
        # Forced tool-free round so the conversation ends cleanly (no
        # orphaned tool_use, no consecutive user turns) and the human gets a
        # summary of what the foreman accomplished before hitting the cap.
        await self._one_round(
            messages, system_blocks, self._cfg.max_rounds, tool_choice={"type": "none"}
        )
        cap_note = f"_(Foreman hit {self._cfg.max_rounds}-round safety cap and stopped.)_"
        await self._journal.text(cap_note)

    async def _one_round(
        self,
        messages: list[dict],
        system_blocks: list[dict],
        round_num: int,
        *,
        tool_choice: dict[str, Any] | None = None,
    ) -> tuple[list[dict], list[Any]]:
        messages = prune_history(messages)
        messages = strip_orphaned_tool_results(messages)
        _stamp_message_cache_breakpoint(messages)
        logger.info(
            "guild=%s ForemanRun round %d: sending %d messages to LLM",
            self._cfg.guild_id,
            round_num,
            len(messages),
        )

        result = await self._llm.call(
            system_blocks=system_blocks,
            messages=messages,
            tools=FOREMAN_TOOLS,
            tool_choice=tool_choice,
            max_tokens=1024,
        )
        turn_id = await self._journal.assistant_turn(result.content, api_log_id=result.api_log_id)
        # Re-parse through the DB serializer so `messages` stays plain dicts
        # (not SDK content-block objects), matching what a reloaded history
        # would contain.
        assistant_content = json.loads(_serialize_content(result.content))
        messages = [*messages, {"role": "assistant", "content": assistant_content}]

        tool_uses = []
        for block in result.content:
            if block.type == "text" and block.text.strip():
                await self._journal.text(block.text.strip())
            elif block.type == "tool_use":
                tool_uses.append(block)
                await self._journal.tool_use(block)

        if not tool_uses:
            return messages, []

        from foreman.runner import _record_guild_action  # noqa: PLC0415 — avoid import cycle

        _record_guild_action(self._cfg.guild_id)

        tool_results = await self._tools.exec(
            self._cfg.guild_id, tool_uses, user_id=self._cfg.user_id
        )
        # Truncate verbose results; filter to only IDs in the current batch so
        # stale results that survived history trimming are never persisted.
        current_tool_use_ids = {tu.id for tu in tool_uses}
        trimmed: list[dict] = []
        for r in tool_results:
            if r.get("tool_use_id") not in current_tool_use_ids:
                continue
            entry = {k: v for k, v in r.items() if k != "api_calls"}
            if entry.get("content"):
                entry = {**entry, "content": truncate_tool_result(entry["content"])}
            trimmed.append(entry)

        for r in trimmed:
            await self._journal.tool_result(r)
        await self._journal.tool_response_turn(trimmed, parent_id=turn_id)
        logger.info(
            "guild=%s round %d: %d tool call(s) dispatched: %s",
            self._cfg.guild_id,
            round_num,
            len(trimmed),
            [
                {"tool_use_id": r["tool_use_id"], "is_error": r.get("is_error", False)}
                for r in trimmed
            ],
        )
        messages = [*messages, {"role": "user", "content": trimmed}]
        return messages, tool_uses
