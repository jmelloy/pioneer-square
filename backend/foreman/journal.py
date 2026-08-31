"""TurnJournal: the single place a Foreman turn's events are written.

Before #1241, one logical event (a text line, a tool_use, a tool_result) was
written by hand at up to three call sites — a WS broadcast, a ``Message`` row,
and (for text) a *different-granularity* concatenated ``Message`` row — inside
``foreman.runner._run_foreman_ai``. Any new field (task_id, thread_id) had to
be threaded through all of them by hand.

``Journal`` splits into two kinds of write, matching two different consumers:

  - **Narration** (``text``/``tool_use``/``tool_result``) — UI-facing. Each
    call does exactly one WS broadcast + one ``Message`` insert, at the same
    granularity (one call per content block). ``Message`` rows now match the
    WS stream one-for-one, so reload and live-stream render identically.
  - **Turn persistence** (``system``/``human``/``assistant_turn``/
    ``tool_response_turn``) — LLM-replay-facing, one ``ForemanTurn`` row per
    round (the API requires all of a round's content blocks in one message),
    no broadcast.

``ForemanReply`` resolves the five overlapping routing parameters
``_emit_foreman_chat`` used to take (task_id, discord_task_id,
discord_channel_id, user_id, thread_id) into one destination, built once per
run and passed to ``TurnJournal``'s constructor — so ``text``/``tool_use``/
``tool_result`` take no routing parameters at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import discord_notifier
from database import get_db
from foreman.ports import Clock, Events, Scheduler
from models import Message
from ws_types import ChatMsg


@dataclass(frozen=True)
class ForemanReply:
    """Resolved destination for one ForemanRun's narration.

    ``discord_task_id`` routes the Discord mirror to that task's thread
    (``None`` skips the task-thread mirror). ``discord_channel_id`` pins the
    mirror to one channel outright, overriding the task-thread lookup — set
    only for a run triggered by an @-mention, so the answer goes back to the
    channel or DM the mention came from (see ``discord_notifier.notify_foreman_chat``).
    """

    guild_id: str
    user_id: str | None
    task_id: str | None
    thread_id: str | None
    discord_task_id: str | None
    discord_channel_id: str | None

    @classmethod
    def for_task(
        cls, guild_id: str, user_id: str | None, task_id: str | None, thread_id: str | None
    ) -> ForemanReply:
        return cls(
            guild_id=guild_id,
            user_id=user_id,
            task_id=task_id,
            thread_id=thread_id,
            discord_task_id=task_id,
            discord_channel_id=None,
        )

    @classmethod
    def for_mention(
        cls, guild_id: str, user_id: str | None, channel_id: str | None, thread_id: str | None
    ) -> ForemanReply:
        return cls(
            guild_id=guild_id,
            user_id=user_id,
            task_id=None,
            thread_id=thread_id,
            discord_task_id=None,
            discord_channel_id=channel_id,
        )


class Journal(Protocol):
    async def system(self, content: Any) -> int: ...

    async def human(self, content: Any) -> int: ...

    async def assistant_turn(self, blocks: Any, *, api_log_id: int | None) -> int: ...

    async def tool_response_turn(self, results: list[dict], *, parent_id: int) -> int: ...

    async def text(self, content: str) -> None: ...

    async def tool_use(self, tool_use: Any) -> None: ...

    async def tool_result(self, result: dict) -> None: ...


class TurnJournal:
    """Production ``Journal``: writes to Postgres, broadcasts over WS, mirrors to Discord."""

    def __init__(
        self,
        *,
        guild_id: str,
        guild_pk: int | None,
        user_id: str | None,
        reply: ForemanReply,
        events: Events,
        scheduler: Scheduler,
        clock: Clock,
    ) -> None:
        self._guild_id = guild_id
        self._guild_pk = guild_pk
        self._user_id = user_id
        self._reply = reply
        self._events = events
        self._scheduler = scheduler
        self._clock = clock

    # -- turn persistence (ForemanTurn rows; no broadcast) -------------------

    async def _save_turn(
        self,
        role: str,
        content: Any,
        *,
        is_tool_response: bool = False,
        parent_id: int | None = None,
        api_log_id: int | None = None,
    ) -> int:
        # Deferred import: foreman.runner._save_turn is the one canonical
        # ForemanTurn-row writer (kept there, not duplicated here, since
        # tests/test_foreman.py calls it directly as a DB-layer primitive
        # independent of any particular run).
        from foreman.runner import _save_turn as _write_turn

        return await _write_turn(
            self._guild_id,
            self._user_id or "",
            role,
            content,
            is_tool_response=is_tool_response,
            parent_id=parent_id,
            api_log_id=api_log_id,
            task_id=self._reply.task_id,
        )

    async def system(self, content: Any) -> int:
        return await self._save_turn("system", content)

    async def human(self, content: Any) -> int:
        return await self._save_turn("user", content)

    async def assistant_turn(self, blocks: Any, *, api_log_id: int | None) -> int:
        return await self._save_turn("assistant", blocks, api_log_id=api_log_id)

    async def tool_response_turn(self, results: list[dict], *, parent_id: int) -> int:
        return await self._save_turn("user", results, is_tool_response=True, parent_id=parent_id)

    # -- narration (WS broadcast + one Message row per block) ---------------

    async def _add_message(self, **kwargs: Any) -> None:
        db = await get_db()
        try:
            db.add(
                Message(
                    guild_id=self._guild_pk or 0,
                    from_agent="foreman",
                    to_agent="user",
                    task_id=self._reply.task_id,
                    thread_id=self._reply.thread_id,
                    **kwargs,
                )
            )
            await db.commit()
        finally:
            await db.close()

    async def text(self, content: str) -> None:
        now = self._clock.now()
        await self._events.broadcast_msg(
            self._guild_id,
            ChatMsg(
                from_="foreman",
                to="user",
                content=content,
                createdAt=now.isoformat(),
                taskId=self._reply.task_id,
                threadId=self._reply.thread_id,
            ),
        )
        self._scheduler.spawn(
            discord_notifier.notify_foreman_chat(
                self._guild_id,
                content,
                task_id=self._reply.discord_task_id,
                channel_id=self._reply.discord_channel_id,
                user_id=self._reply.user_id,
            ),
            name=f"discord.foreman-chat:{self._guild_id}",
        )
        await self._add_message(
            content=content,
            message_type="chat",
            created_at=now,
            user_id=self._reply.user_id,
            source="a2a" if self._reply.user_id and "." in self._reply.user_id else "web",
        )

    async def tool_use(self, tool_use: Any) -> None:
        now = self._clock.now()
        tool_input = dict(tool_use.input) if tool_use.input else {}
        await self._events.broadcast_msg(
            self._guild_id,
            ChatMsg(
                from_="foreman",
                role="tool_use",
                to="user",
                content=f"▶ {tool_use.name}",
                toolName=tool_use.name,
                toolInput=tool_input,
                toolId=tool_use.id,
                createdAt=now.isoformat(),
                taskId=self._reply.task_id,
                threadId=self._reply.thread_id,
            ),
        )
        await self._add_message(
            content=f"▶ {tool_use.name}",
            message_type="chat",
            role="tool_use",
            meta=json.dumps(
                {"toolId": tool_use.id, "toolName": tool_use.name, "toolInput": tool_input}
            ),
            created_at=now,
        )

    async def tool_result(self, result: dict) -> None:
        now = self._clock.now()
        content = result.get("content", "") or ""
        is_error = result.get("is_error", False)
        await self._events.broadcast_msg(
            self._guild_id,
            ChatMsg(
                from_="foreman",
                role="tool_result",
                to="user",
                content=content,
                toolId=result.get("tool_use_id"),
                toolOutput=content,
                isError=is_error,
                createdAt=now.isoformat(),
                taskId=self._reply.task_id,
                threadId=self._reply.thread_id,
            ),
        )
        await self._add_message(
            content=content,
            message_type="chat",
            role="tool_result",
            meta=json.dumps({"toolId": result.get("tool_use_id"), "isError": is_error}),
            created_at=now,
        )
