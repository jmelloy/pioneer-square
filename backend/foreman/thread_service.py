"""Foreman-owned thread lifecycle (issue #1167).

Corrects the architecture introduced in #1163/#1165 (see epic #1160's
"Architectural correction" comment): a :class:`models.Thread` is a Foreman
construct, created/reused as a side effect of the Foreman handling a
message, never something Discord or the frontend originates. Downstream
mirrors (Discord bot #1168, frontend #1169) subscribe to the REST
(``routes/threads.py``) and WebSocket (``thread-created``/``thread-updated``)
surface this module keeps current — they never drive thread creation or
status transitions themselves.

``ensure_conversation_thread`` is the single thread-creation entry point,
called from ``ws_handlers._trigger_foreman`` for every human-originated
message that isn't already scoped to an existing task. ``get_or_create_active_thread``
is the lower-level, session-scoped version used both there and from
``foreman/tools.py`` (to stamp ``Task.thread_id`` at task-creation time) —
calling it twice within the same logical turn is safe and idempotent: the
second call just reuses the thread the first call already created.
"""

from __future__ import annotations

import logging
import random
import string
from datetime import UTC, datetime

from database import get_db
from events import broadcast
from models import Conversation, Task, Thread
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import ThreadCreatedMsg, ThreadUpdatedMsg

logger = logging.getLogger(__name__)

_MAX_NAME_LEN = 80


def _new_thread_id() -> str:
    return "th-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _name_from_message(content: str | None) -> str | None:
    text = " ".join((content or "").split())
    if not text:
        return None
    return text if len(text) <= _MAX_NAME_LEN else text[: _MAX_NAME_LEN - 1] + "…"


async def get_or_create_conversation(db: AsyncSession, guild_pk: int, user_id: str) -> Conversation:
    """Get-or-create the one :class:`Conversation` for this (guild, user) pair.

    Mirrors the "one conversation per (guild, user)" shape ``ForemanTurn``
    history already assumes (see ``Conversation``'s docstring in models.py).
    """
    result = await db.exec(
        select(Conversation).where(
            col(Conversation.guild_id) == guild_pk, col(Conversation.user_id) == user_id
        )
    )
    conversation = result.first()
    if conversation is not None:
        return conversation
    now = datetime.now(UTC)
    conversation = Conversation(guild_id=guild_pk, user_id=user_id, created_at=now, updated_at=now)
    db.add(conversation)
    await db.flush()
    return conversation


async def get_or_create_active_thread(
    db: AsyncSession, guild_pk: int, user_id: str, *, name_hint: str | None = None
) -> tuple[Thread, bool]:
    """Return ``(thread, created)`` for this user's current conversation.

    Reuses the most recently active thread for the conversation if one
    exists (bumping ``updated_at`` so the idle sweep — see
    ``foreman/thread_maintenance.py`` — doesn't archive an in-progress
    conversation out from under it); otherwise creates a fresh
    Conversation + Thread. This is the sole Thread-creation path (#1167): a
    thread is always a side effect of the Foreman handling a message.
    """
    conversation = await get_or_create_conversation(db, guild_pk, user_id)
    result = await db.exec(
        select(Thread)
        .where(
            col(Thread.conversation_id) == conversation.id,
            col(Thread.status) == "active",
            col(Thread.deleted_at).is_(None),
        )
        .order_by(col(Thread.updated_at).desc())
        .limit(1)
    )
    thread = result.first()
    now = datetime.now(UTC)
    if thread is not None:
        thread.updated_at = now
        db.add(thread)
        await db.commit()
        await db.refresh(thread)
        return thread, False

    thread = Thread(
        id=_new_thread_id(),
        conversation_id=conversation.id,
        name=_name_from_message(name_hint),
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return thread, True


async def get_thread_for_task(db: AsyncSession, task: Task) -> Thread | None:
    """Return the :class:`Thread` a task was created from, or None."""
    if not task.thread_id:
        return None
    result = await db.exec(
        select(Thread).where(col(Thread.id) == task.thread_id, col(Thread.deleted_at).is_(None))
    )
    return result.first()


async def broadcast_thread_updated(db: AsyncSession, thread: Thread) -> None:
    """Broadcast a ``thread-updated`` WS event and mirror to Discord.

    Resolves the guild slug via Thread -> Conversation -> Guild. Best
    effort — swallows and logs lookup/broadcast failures so a WS hiccup
    never blocks the caller's own transition, matching every other
    Gateway/sweep handler in this codebase.

    Also triggers the Discord thread mirror (issue #1168) to archive/
    un-archive the corresponding Discord thread when Foreman changes status.
    """
    try:
        from models import Guild  # noqa: PLC0415 — avoid import cycle at module load

        result = await db.exec(
            select(col(Guild.slug))
            .join(Conversation, col(Conversation.guild_id) == col(Guild.id))
            .where(col(Conversation.id) == thread.conversation_id)
        )
        guild_slug = result.first()
        if not guild_slug:
            return
        await broadcast(
            guild_slug,
            ThreadUpdatedMsg(
                threadId=thread.id,
                status=thread.status,
                discordThreadId=thread.discord_thread_id,
                deletedAt=thread.deleted_at.isoformat() if thread.deleted_at else None,
            ).model_dump(by_alias=True, exclude_none=True),
        )

        # Mirror status change to Discord (issue #1168)
        try:
            from discord.thread_mirror import on_thread_updated  # noqa: PLC0415

            await on_thread_updated(
                thread_id=thread.id,
                status=thread.status,
                deleted_at=thread.deleted_at.isoformat() if thread.deleted_at else None,
            )
        except Exception:
            logger.warning(
                "thread_service: Discord mirror update failed thread=%s",
                thread.id,
                exc_info=True,
            )
    except Exception:
        logger.warning(
            "thread_service: failed to broadcast thread-updated thread=%s", thread.id, exc_info=True
        )


async def ensure_conversation_thread(
    guild_slug: str, user_id: str, content: str | None = None
) -> Thread | None:
    """Get-or-create the active thread for (guild_slug, user_id).

    The entry point ``ws_handlers._trigger_foreman`` calls for every
    human-originated message with no ``task_id`` yet — the "sending the
    Foreman a message automatically creates a new thread" side effect
    (#1167). Broadcasts ``thread-created`` the first time a thread is
    created so downstream mirrors can subscribe rather than poll. Returns
    None if the guild can't be resolved. Never raises — callers treat
    thread bookkeeping as best-effort, never a reason to drop a message.
    """
    from auth_deps import get_guild_pk  # noqa: PLC0415 — avoid import cycle at module load

    try:
        db = await get_db()
        try:
            guild_pk = await get_guild_pk(db, guild_slug)
            if guild_pk is None:
                return None
            thread, created = await get_or_create_active_thread(
                db, guild_pk, user_id, name_hint=content
            )
        finally:
            await db.close()
    except Exception:
        logger.warning(
            "thread_service: failed to ensure conversation thread guild=%s user=%s",
            guild_slug,
            user_id,
            exc_info=True,
        )
        return None

    if created:
        try:
            await broadcast(
                guild_slug,
                ThreadCreatedMsg(
                    threadId=thread.id,
                    conversationId=thread.conversation_id,
                    userId=user_id,
                    name=thread.name,
                    status=thread.status,
                    createdAt=thread.created_at.isoformat(),
                ).model_dump(by_alias=True, exclude_none=True),
            )
        except Exception:
            logger.warning(
                "thread_service: failed to broadcast thread-created thread=%s",
                thread.id,
                exc_info=True,
            )

        # Mirror to Discord (issue #1168): create a Discord thread only
        # in response to the Foreman creating one — never independently.
        try:
            from discord.thread_mirror import on_thread_created  # noqa: PLC0415

            await on_thread_created(
                thread_id=thread.id,
                conversation_id=thread.conversation_id,
                guild_slug=guild_slug,
                name=thread.name,
                user_id=user_id,
            )
        except Exception:
            logger.warning(
                "thread_service: Discord mirror failed for thread=%s",
                thread.id,
                exc_info=True,
            )
    return thread
