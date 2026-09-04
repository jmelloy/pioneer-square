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
called from ``foreman.triggers.trigger_foreman`` for every human-originated
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
from foreman.conversation_service import get_or_create_conversation
from models import Conversation, Task, Thread
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import ThreadCreatedMsg, ThreadUpdatedMsg

logger = logging.getLogger(__name__)

_MAX_NAME_LEN = 80

# Re-exported for existing importers (foreman.thread_service.get_or_create_conversation);
# the canonical definition now lives in foreman.conversation_service (#1271) so
# it can be shared without importing this module's Thread/Discord side effects.
__all__ = [
    "ensure_conversation_thread",
    "get_or_create_active_thread",
    "get_or_create_conversation",
    "get_thread_for_task",
    "reactivate_conversation_thread",
    "resolve_thread_id",
]


def _new_thread_id() -> str:
    return "th-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _name_from_message(content: str | None) -> str | None:
    text = " ".join((content or "").split())
    if not text:
        return None
    return text if len(text) <= _MAX_NAME_LEN else text[: _MAX_NAME_LEN - 1] + "…"


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
        _sync_conversation_from_thread(conversation, thread, now)
        db.add(conversation)
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
    _sync_conversation_from_thread(conversation, thread, now)
    db.add(conversation)
    await db.commit()
    await db.refresh(thread)
    return thread, True


def _sync_conversation_from_thread(
    conversation: Conversation, thread: Thread, now: datetime
) -> None:
    """Mirror ``thread``'s UI/lifecycle fields onto its owning ``conversation``.

    ``Conversation.name/status/discord_thread_id`` (#1274) always reflect
    the conversation's *currently active* thread. Safe to call unconditionally
    here — a thread is only ever created/reused as *the* active thread for its
    conversation, so there's no risk of mirroring a stale/superseded thread.
    """
    conversation.name = thread.name
    conversation.status = thread.status
    conversation.discord_thread_id = thread.discord_thread_id
    conversation.updated_at = now


async def sync_conversation_after_thread_update(
    db: AsyncSession, thread: Thread, *, previous_status: str | None = None
) -> None:
    """Mirror a mutated ``thread`` back onto its ``Conversation`` (#1274).

    For callers outside this module that mutate an existing ``Thread`` row
    directly — ``routes/threads.py``'s archive/close endpoints,
    ``foreman/thread_maintenance.py``'s idle sweep, and
    ``discord/thread_mirror.py`` stamping ``discord_thread_id`` after Discord
    confirms thread creation.

    A conversation can outlive many threads (see ``Thread``'s docstring): by
    the time an old thread is swept from "archived" to "closed", a newer
    thread may have already superseded it as the conversation's active one.
    When *previous_status* is given, the mirror only applies if the
    conversation's status still matches it — i.e. the conversation hasn't
    already moved on. Pass ``previous_status=None`` (the default) when the
    caller knows the thread is still current (e.g. right after creating it
    or stamping its ``discord_thread_id``).
    """
    conversation = await db.get(Conversation, thread.conversation_id)
    if conversation is None:
        return
    if previous_status is not None and conversation.status != previous_status:
        return
    _sync_conversation_from_thread(conversation, thread, thread.updated_at)
    db.add(conversation)


async def resolve_thread_id(
    db: AsyncSession, guild_pk: int, *, task_id: str | None = None, user_id: str | None = None
) -> str | None:
    """Best-effort, read-only lookup of the Thread a new message belongs to.

    Used to stamp ``Message.thread_id`` at persist time (#1175) so the frontend
    can show each thread its own conversation history. Prefers *task_id*'s
    thread (stamped once at task-creation time — see ``foreman/tools.py``) since
    that's an explicit, stable binding; otherwise falls back to *user_id*'s
    current active thread, if one already exists. Never creates a thread — that
    side effect belongs exclusively to ``ensure_conversation_thread``/
    ``get_or_create_active_thread``, so a message sent before any thread exists
    for its conversation (e.g. the very first human line, or a purely
    automated/system message) is simply left unthreaded.
    """
    if task_id:
        result = await db.exec(select(col(Task.thread_id)).where(col(Task.id) == task_id))
        thread_id = result.first()
        if thread_id:
            return thread_id
    if user_id:
        result = await db.exec(
            select(col(Thread.id))
            .join(Conversation, col(Thread.conversation_id) == col(Conversation.id))
            .where(
                col(Conversation.guild_id) == guild_pk,
                col(Conversation.user_id) == user_id,
                col(Thread.status) == "active",
                col(Thread.deleted_at).is_(None),
            )
            .order_by(col(Thread.updated_at).desc())
            .limit(1)
        )
        return result.first()
    return None


async def get_thread_for_task(db: AsyncSession, task: Task) -> Thread | None:
    """Return the :class:`Thread` a task was created from, or None."""
    if not task.thread_id:
        return None
    result = await db.exec(
        select(Thread).where(col(Thread.id) == task.thread_id, col(Thread.deleted_at).is_(None))
    )
    return result.first()


async def _active_thread_for_conversation(db: AsyncSession, conversation_id: int) -> Thread | None:
    """Return *conversation*'s current active (non-deleted) thread, if any."""
    result = await db.exec(
        select(Thread)
        .where(
            col(Thread.conversation_id) == conversation_id,
            col(Thread.status) == "active",
            col(Thread.deleted_at).is_(None),
        )
        .order_by(col(Thread.updated_at).desc())
        .limit(1)
    )
    return result.first()


async def reactivate_conversation_thread(
    db: AsyncSession, conversation: Conversation, discord_thread_id: str
) -> Thread | None:
    """Reactivate the specific thread a Discord reply landed in (issue #1278).

    A reply posted into an existing Discord thread should continue *that*
    conversation even if the Foreman's idle sweep (``thread_maintenance``)
    had already archived it — Discord itself un-archives a thread the moment
    someone posts in it, so leaving ``Thread.status``/``Conversation.status``
    at "archived" would desync them from what the user is looking at and
    would make the next top-level message fork off a brand new Discord
    thread instead of continuing this one (``get_or_create_active_thread``
    only ever looks at the *active* thread).

    Looked up by ``(conversation_id, discord_thread_id)`` rather than just
    ``discord_thread_id`` alone since the caller (``discord.router``) has
    already resolved *conversation* via
    ``conversation_service.get_conversation_by_discord_thread_id`` — the
    authoritative Discord binding lives on ``Conversation``, this only needs
    the matching ``Thread`` row to update the per-instance record and drive
    the Discord un-archive call. Returns None if no such Thread row exists
    (soft-deleted or never created), in which case the caller falls back to
    ``get_or_create_active_thread``.
    """
    result = await db.exec(
        select(Thread)
        .where(
            col(Thread.conversation_id) == conversation.id,
            col(Thread.discord_thread_id) == discord_thread_id,
            col(Thread.deleted_at).is_(None),
        )
        .order_by(col(Thread.updated_at).desc())
        .limit(1)
    )
    thread = result.first()
    if thread is None:
        return None

    now = datetime.now(UTC)
    was_archived = thread.status != "active"
    thread.status = "active"
    thread.updated_at = now
    db.add(thread)
    _sync_conversation_from_thread(conversation, thread, now)
    db.add(conversation)
    await db.commit()
    await db.refresh(thread)

    if was_archived:
        try:
            from discord.thread_mirror import on_thread_updated  # noqa: PLC0415

            await on_thread_updated(thread_id=thread.id, status="active")
        except Exception:
            logger.warning(
                "thread_service: failed to un-archive Discord thread for reactivated "
                "conversation=%s thread=%s",
                conversation.id,
                thread.id,
                exc_info=True,
            )

    return thread


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

    The entry point ``foreman.triggers.trigger_foreman`` calls for every
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
