"""ConversationHistory: the one windowing implementation for Foreman turns.

Before #1241, ``foreman.runner._load_history`` (feeds the LLM) and
``foreman.runner.get_foreman_history`` (feeds the debug pane) each hand-rolled
the same human-turn-window backward scan and "trim until starts with user"
loop, with comments pointing at each other ("mirrors _load_history") instead
of a shared implementation. ``ConversationHistory`` is that shared
implementation: ``_windowed_turns`` is the one place the fetch-limit +
backward-scan cutoff logic lives, and ``load_for_llm``/``load_for_debug`` are
its two callers, shaping the same windowed slice for their own contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, TypedDict

from auth_deps import get_guild_pk
from database import get_db
from foreman.constants import _HUMAN_TURN_WINDOW
from foreman.message_utils import prune_history, strip_orphaned_tool_results
from models import ForemanTurn
from sqlmodel import col, select

# Upper bound on rows fetched before Python-side windowing, so query cost
# stays flat regardless of the table's total lifetime turn count.
_HISTORY_FETCH_LIMIT = 100


class DebugHistory(TypedDict):
    system: str | None
    messages: list[dict]
    total: int


class History(Protocol):
    async def load_for_llm(self, guild_id: str, user_id: str) -> list[dict]: ...

    async def load_for_debug(self, guild_id: str, user_id: str) -> DebugHistory: ...


@dataclass
class _Window:
    """The fetched turns (oldest→newest, capped at ``_HISTORY_FETCH_LIMIT``)
    plus the index the human-turn-window cutoff lands on."""

    turns: list[ForemanTurn]
    cutoff: int


class ConversationHistory:
    """Production ``History``, backed by the ``foreman_turns`` table."""

    async def _windowed_turns(self, guild_id: str, user_id: str) -> _Window:
        """Fetch the most recent ``_HISTORY_FETCH_LIMIT`` turns for (guild, user)
        and find the backward-scan cutoff: the index of the
        ``_HUMAN_TURN_WINDOW``-th-from-last non-tool-response user turn.

        Because the cutoff always lands on a human-initiated user turn, every
        assistant-turn / tool_result-user-turn pair that follows it is
        guaranteed to be included intact — no orphaned tool_use blocks, no
        synthetic repairs needed.
        """
        db = await get_db()
        try:
            guild_pk_val = await get_guild_pk(db, guild_id)
            stmt = select(ForemanTurn).where(
                col(ForemanTurn.guild_id) == guild_pk_val, col(ForemanTurn.user_id) == user_id
            )
            result = await db.exec(
                stmt.order_by(col(ForemanTurn.id).desc()).limit(_HISTORY_FETCH_LIMIT)
            )
            turns = list(reversed(result.all()))
        finally:
            await db.close()

        cutoff = 0
        human_count = 0
        for i in range(len(turns) - 1, -1, -1):
            t = turns[i]
            if t.role == "user" and not t.is_tool_response:
                human_count += 1
                if human_count >= _HUMAN_TURN_WINDOW:
                    cutoff = i
                    break
        return _Window(turns=turns, cutoff=cutoff)

    async def load_for_llm(self, guild_id: str, user_id: str) -> list[dict]:
        """Windowed turns -> plain {role, content} dicts for the Anthropic API.

        System turns are excluded — they're persisted for auditing but must
        not appear in the messages array (the system prompt is a top-level
        API param, not a message). Leading non-user turns are trimmed since
        the API requires the first message to have role "user".
        """
        window = await self._windowed_turns(guild_id, user_id)
        messages = [
            {"role": t.role, "content": json.loads(t.content_json)}
            for t in window.turns[window.cutoff :]
            if t.role != "system"
        ]
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        return messages

    async def load_for_debug(self, guild_id: str, user_id: str) -> DebugHistory:
        """Same windowed turns, full metadata, then the same
        prune_history + strip_orphaned_tool_results pipeline ForemanRun's
        loop applies before every real API call — so the debug pane shows
        exactly what round 0 would send.
        """
        window = await self._windowed_turns(guild_id, user_id)
        turns = window.turns

        # Most-recent system turn only (there's one per invocation; showing
        # every one would just duplicate near-identical audit text).
        system_content: str | None = None
        for t in reversed(turns):
            if t.role == "system":
                raw = json.loads(t.content_json)
                system_content = raw if isinstance(raw, str) else json.dumps(raw)
                break

        total = sum(1 for t in turns if t.role != "system")
        if not turns:
            return {"system": system_content, "messages": [], "total": 0}

        messages: list[dict] = [
            {
                "id": t.id,
                "role": t.role,
                "is_tool_response": bool(t.is_tool_response),
                "parent_id": t.parent_id,
                "content": json.loads(t.content_json),
                "created_at": t.created_at,
            }
            for t in turns[window.cutoff :]
            if t.role != "system"
        ]
        while messages and messages[0]["role"] != "user":
            messages.pop(0)

        messages = prune_history(messages)
        messages = strip_orphaned_tool_results(messages)
        return {"system": system_content, "messages": messages, "total": total}
