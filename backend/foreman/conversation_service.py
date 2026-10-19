"""Conversation lifecycle and resolution (issues #1271, #1299).

:class:`models.Conversation` is the core Foreman thread model: this module
resolves/creates conversations, owns their lifecycle (create / reactivate /
rename / close) and fans each change out to the frontend (``conversation-*``
WS events). The legacy ``Thread`` model is no longer involved — Discord
threads are a pure mirror, bound via ``Conversation.discord_thread_id`` and
driven by ``discord.thread_mirror``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from events import broadcast
from models import Conversation, Guild, Task
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import ConversationCreatedMsg, ConversationUpdatedMsg

logger = logging.getLogger(__name__)


async def _guild_slug(db: AsyncSession, guild_pk: int) -> str | None:
    result = await db.exec(select(col(Guild.slug)).where(col(Guild.id) == guild_pk))
    return result.first()


async def _broadcast_conversation_created(db: AsyncSession, conversation: Conversation) -> None:
    """Broadcast a ``conversation-created`` WS event (#1298). Best-effort.

    Swallows and logs failures so a WS hiccup never blocks conversation
    creation.
    """
    try:
        guild_slug = await _guild_slug(db, conversation.guild_id)
        if not guild_slug:
            return
        await broadcast(
            guild_slug,
            ConversationCreatedMsg(
                conversationId=conversation.id,
                userId=conversation.user_id,
                name=conversation.name,
                status=conversation.status,
                createdAt=conversation.created_at.isoformat(),
            ).model_dump(by_alias=True, exclude_none=True),
        )
    except Exception:
        logger.warning(
            "conversation_service: failed to broadcast conversation-created conversation=%s",
            conversation.id,
            exc_info=True,
        )


async def broadcast_conversation_updated(db: AsyncSession, conversation: Conversation) -> None:
    """Broadcast a ``conversation-updated`` WS event (#1298). Best-effort.

    Public (not ``_``-prefixed) because the idle sweep
    (``foreman.conversation_maintenance``) and ``discord.thread_mirror`` also
    call it — every path that mutates a conversation needs the frontend's
    ``conversations`` store to learn about it.
    """
    try:
        guild_slug = await _guild_slug(db, conversation.guild_id)
        if not guild_slug:
            return
        await broadcast(
            guild_slug,
            ConversationUpdatedMsg(
                conversationId=conversation.id,
                name=conversation.name,
                status=conversation.status,
                discordThreadId=conversation.discord_thread_id,
            ).model_dump(by_alias=True, exclude_none=True),
        )
    except Exception:
        logger.warning(
            "conversation_service: failed to broadcast conversation-updated conversation=%s",
            conversation.id,
            exc_info=True,
        )


async def _latest_conversation(
    db: AsyncSession, guild_pk: int, user_id: str
) -> Conversation | None:
    """Most recently updated conversation for (guild, user), or None."""
    result = await db.exec(
        select(Conversation)
        .where(col(Conversation.guild_id) == guild_pk, col(Conversation.user_id) == user_id)
        .order_by(col(Conversation.updated_at).desc())
        .limit(1)
    )
    return result.first()


async def get_or_create_conversation(db: AsyncSession, guild_pk: int, user_id: str) -> Conversation:
    """Get-or-create *a* :class:`Conversation` for this (guild, user) pair.

    Historically ``Conversation`` was 1:1 with (guild_id, user_id) (see the
    class docstring in models.py); issue #1296 broke that assumption — a new
    top-level human Foreman message now always starts a fresh conversation
    (see :func:`create_conversation`), so a (guild, user) pair can have many
    ``Conversation`` rows over time. This helper keeps its original
    get-or-create semantics for existing callers that still want "the"
    conversation for a pair (picking the most recently updated one when more
    than one exists).

    Does not commit, bump ``updated_at`` or reactivate — use
    :func:`get_or_create_active_conversation` for those semantics.
    """
    conversation = await _latest_conversation(db, guild_pk, user_id)
    if conversation is not None:
        return conversation
    return await create_conversation(db, guild_pk, user_id)


async def create_conversation(
    db: AsyncSession, guild_pk: int, user_id: str | None, *, name: str | None = None
) -> Conversation:
    """Unconditionally insert a brand-new :class:`Conversation` row (#1296).

    Unlike :func:`get_or_create_conversation`, never reuses an existing row —
    used to start a fresh conversation for a new top-level human Foreman
    message (one that isn't a reply within an existing Discord thread/
    conversation; see ``discord.router._persist_inbound_message`` and
    :func:`get_or_create_active_conversation`'s ``force_new`` parameter), so that conversation stays independent of whatever other
    conversation(s) this (guild, user) pair already has open. Flushes but does
    not commit — same contract as ``get_or_create_conversation``.
    """
    now = datetime.now(UTC)
    conversation = Conversation(
        guild_id=guild_pk, user_id=user_id, name=name, created_at=now, updated_at=now
    )
    db.add(conversation)
    await db.flush()
    await _broadcast_conversation_created(db, conversation)
    return conversation


_MAX_NAME_LEN = 80


def name_from_message(content: str | None) -> str | None:
    """Derive a short conversation name from the first message's text."""
    text = " ".join((content or "").split())
    if not text:
        return None
    return text if len(text) <= _MAX_NAME_LEN else text[: _MAX_NAME_LEN - 1] + "…"


async def get_or_create_active_conversation(
    db: AsyncSession,
    guild_pk: int,
    user_id: str,
    *,
    name_hint: str | None = None,
    force_new: bool = False,
) -> tuple[Conversation, bool]:
    """Return ``(conversation, created)`` for this user's current conversation.

    Reuses the user's most recently updated conversation, bumping
    ``updated_at`` (so the idle sweep in ``foreman.conversation_maintenance``
    doesn't archive an in-progress conversation) and reactivating it if the
    sweep already archived/closed it — rather than forking a brand-new
    conversation (and Discord thread) per idle/resume cycle (#1314). Only
    creates a new conversation when the user has none yet.

    ``force_new=True`` (#1296) skips reuse: it always inserts a brand-new
    conversation, for a top-level human message that isn't a reply within an
    existing conversation. Commits *db*.
    """
    name = name_from_message(name_hint)
    if force_new:
        conversation = await create_conversation(db, guild_pk, user_id, name=name)
        await db.commit()
        return conversation, True

    conversation = await _latest_conversation(db, guild_pk, user_id)
    if conversation is None:
        conversation = await create_conversation(db, guild_pk, user_id, name=name)
        await db.commit()
        return conversation, True

    reactivated = conversation.status != "active"
    conversation.status = "active"
    if conversation.name is None:
        conversation.name = name
    conversation.updated_at = datetime.now(UTC)
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    if reactivated:
        await broadcast_conversation_updated(db, conversation)
        await _mirror_status_to_discord(conversation)
    return conversation, False


async def _mirror_status_to_discord(conversation: Conversation) -> None:
    """Archive/un-archive the conversation's Discord thread to match its status."""
    if not conversation.discord_thread_id:
        return
    try:
        from discord.thread_mirror import on_thread_updated  # noqa: PLC0415

        await on_thread_updated(conversation.discord_thread_id, status=conversation.status)
    except Exception:
        logger.warning(
            "conversation_service: Discord mirror update failed conversation=%s",
            conversation.id,
            exc_info=True,
        )


async def reactivate_conversation(db: AsyncSession, conversation: Conversation) -> None:
    """Reactivate *conversation* after a reply landed in its Discord thread (#1278).

    Discord un-archives a thread the moment someone posts in it, so leaving
    the conversation "archived" would desync it and make the next top-level
    message fork a new conversation. No-op status-wise if already active
    (still bumps ``updated_at``). Commits *db*.
    """
    was_inactive = conversation.status != "active"
    conversation.status = "active"
    conversation.updated_at = datetime.now(UTC)
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    if was_inactive:
        await broadcast_conversation_updated(db, conversation)
        await _mirror_status_to_discord(conversation)


async def set_conversation_status(
    db: AsyncSession, conversation: Conversation, status: str
) -> None:
    """Set *conversation*'s lifecycle status, then broadcast and mirror to Discord.

    Commits *db*. Used by the idle sweep (archive/close).
    """
    conversation.status = status
    conversation.updated_at = datetime.now(UTC)
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    await broadcast_conversation_updated(db, conversation)
    await _mirror_status_to_discord(conversation)


async def ensure_conversation(
    guild_slug: str, user_id: str, content: str | None = None, *, force_new: bool = False
) -> Conversation | None:
    """Get-or-create the active conversation for (guild_slug, user_id).

    The entry point ``foreman.triggers.trigger_foreman`` calls for every
    human-originated message with no ``task_id`` yet. Mirrors a newly created
    conversation to Discord (a Discord thread is only ever created in
    response to the Foreman creating a conversation). Returns None if the
    guild can't be resolved. Never raises — callers treat conversation
    bookkeeping as best-effort, never a reason to drop a message.
    """
    from auth_deps import get_guild_pk  # noqa: PLC0415 — avoid import cycle at module load
    from database import get_db  # noqa: PLC0415

    try:
        db = await get_db()
        try:
            guild_pk = await get_guild_pk(db, guild_slug)
            if guild_pk is None:
                return None
            conversation, created = await get_or_create_active_conversation(
                db, guild_pk, user_id, name_hint=content, force_new=force_new
            )
        finally:
            await db.close()
    except Exception:
        logger.warning(
            "conversation_service: failed to ensure conversation guild=%s user=%s",
            guild_slug,
            user_id,
            exc_info=True,
        )
        return None

    if created:
        try:
            from discord.thread_mirror import on_conversation_created  # noqa: PLC0415

            await on_conversation_created(
                conversation_id=conversation.id,
                guild_slug=guild_slug,
                name=conversation.name,
                user_id=user_id,
            )
        except Exception:
            logger.warning(
                "conversation_service: Discord mirror failed for conversation=%s",
                conversation.id,
                exc_info=True,
            )
    return conversation


async def resolve_conversation_id(
    db: AsyncSession, guild_pk: int, *, task_id: str | None = None, user_id: str | None = None
) -> int | None:
    """Resolve the :class:`Conversation` id a new message/turn/task belongs to.

    Prefers *task_id*'s already-stamped conversation (an explicit, stable
    binding set at task-creation time) over *user_id*'s conversation, creating
    one when only *user_id* is given.

    Does **not** commit *db*. When it creates a new conversation the insert
    is only flushed, so the caller owns the transaction and must
    ``await db.commit()`` before the session is ``close()``'d — otherwise the
    row is silently rolled back and every row stamped with the returned
    ``conversation_id`` becomes a dangling FK that fails at a *later*
    session's commit. Callers that go on to write and commit in the same
    session (e.g. creating a ``Task``) get that for free.
    """
    if task_id:
        result = await db.exec(select(col(Task.conversation_id)).where(col(Task.id) == task_id))
        conversation_id = result.first()
        if conversation_id is not None:
            return conversation_id
    if user_id:
        conversation = await get_or_create_conversation(db, guild_pk, user_id)
        return conversation.id
    return None


async def touch_conversation(db: AsyncSession, conversation: Conversation) -> None:
    """Bump a conversation's ``updated_at`` to now and commit."""
    conversation.updated_at = datetime.now(UTC)
    db.add(conversation)
    await db.commit()


async def get_conversation_by_discord_thread_id(
    db: AsyncSession, guild_pk: int, discord_thread_id: str
) -> Conversation | None:
    """Find the conversation bound to a Discord thread (issue #1278).

    ``Conversation.discord_thread_id`` is the source of truth for a
    conversation's Discord binding (see its docstring in ``models.py``) —
    this is the direct lookup path a reply posted into an existing Discord
    thread uses to find its conversation, instead of re-deriving it from the
    (guild, user) active-thread heuristic (see ``discord.router``). Still
    returns a conversation whose mirrored thread has since gone
    archived/closed — see :func:`reactivate_conversation`, which callers use
    to un-archive it when a user replies there anyway.
    """
    result = await db.exec(
        select(Conversation).where(
            col(Conversation.guild_id) == guild_pk,
            col(Conversation.discord_thread_id) == discord_thread_id,
        )
    )
    return result.first()


async def rename_conversation(db: AsyncSession, conversation: Conversation, name: str) -> None:
    """Rename *conversation* and mirror the new name onto its Discord thread (#1278).

    Commits *db* itself.
    """
    now = datetime.now(UTC)
    conversation.name = name
    conversation.updated_at = now
    db.add(conversation)

    await db.commit()
    await broadcast_conversation_updated(db, conversation)

    if conversation.discord_thread_id:
        from discord.thread_mirror import rename_conversation_thread  # noqa: PLC0415

        await rename_conversation_thread(conversation.discord_thread_id, name)


async def close_conversation(db: AsyncSession, conversation: Conversation) -> None:
    """Close *conversation* and archive its mirrored Discord thread (#1278).

    Archives Discord directly from ``Conversation.discord_thread_id``, the
    source of truth for the binding. Commits *db* itself.
    """
    now = datetime.now(UTC)
    conversation.status = "closed"
    conversation.updated_at = now
    db.add(conversation)

    await db.commit()
    await broadcast_conversation_updated(db, conversation)

    if conversation.discord_thread_id:
        from discord.thread_mirror import archive_conversation_thread_by_id  # noqa: PLC0415

        await archive_conversation_thread_by_id(conversation.discord_thread_id)


async def close_active_conversation_for_user(guild_slug: str, user_id: str) -> None:
    """Close *user_id*'s active conversation in *guild_slug*, if one exists (#1288).

    Replaces the old ``discord_notifier.archive_conversation_thread``, which
    archived a Discord thread keyed by a legacy ``"<guild>:<user_id>"``
    subject — a key Foreman-managed conversation threads (bound via
    ``Conversation.discord_thread_id``, not that legacy table) never wrote,
    so it silently did nothing for any conversation created after #1167.
    Called by ``routes/foreman.py:clear_foreman_context`` — clearing a user's
    Foreman history is the closest existing "conversation closed" signal, so
    this both marks the ``Conversation`` closed and archives its mirrored
    Discord thread via :func:`close_conversation`. Silent no-op if the guild
    or an active conversation can't be resolved. Never raises.
    """
    from auth_deps import get_guild_pk  # noqa: PLC0415
    from database import AsyncSessionLocal  # noqa: PLC0415

    try:
        async with AsyncSessionLocal() as db:
            guild_pk = await get_guild_pk(db, guild_slug)
            if guild_pk is None:
                return
            result = await db.exec(
                select(Conversation)
                .where(
                    col(Conversation.guild_id) == guild_pk,
                    col(Conversation.user_id) == user_id,
                    col(Conversation.status) == "active",
                )
                .order_by(col(Conversation.updated_at).desc())
                .limit(1)
            )
            conversation = result.first()
            if conversation is None:
                return
            await close_conversation(db, conversation)
    except Exception:
        logger.warning(
            "conversation_service: failed to close active conversation guild=%s user=%s",
            guild_slug,
            user_id,
            exc_info=True,
        )
