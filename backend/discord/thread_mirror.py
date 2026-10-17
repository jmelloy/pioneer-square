"""Discord thread mirroring of Foreman Conversations (issues #1168, #1299).

This module is the single integration point between a Conversation's
lifecycle and Discord's thread representation. The Foreman owns the
Conversation; Discord is a mirror/transport detail — the Discord bot never
creates threads independently.

Architecture:
    - ``foreman/conversation_service.py`` creates/manages conversations
    - This module observes those state changes and creates/updates Discord
      threads to match
    - Human messages in Discord threads are forwarded inward to the Foreman
      (via ``discord/router.py``) — this module does NOT handle inbound
    - Discord holds NO independent state: ``Conversation.discord_thread_id``
      is the only link, and it's written here after Discord confirms thread
      creation

Entry points:
    ``on_conversation_created`` — called when the Foreman creates a new
        conversation; the only place that stamps
        ``Conversation.discord_thread_id``
    ``on_thread_updated``  — called when the Foreman changes a conversation's
        status, given the Discord thread id directly by the caller
    ``relay_discord_thread_event`` — relays Discord-side archive/delete
        events inward without treating them as authoritative state changes
    ``rename_conversation_thread`` / ``archive_conversation_thread_by_id`` —
        mirror a Conversation-driven rename/close straight from
        ``Conversation.discord_thread_id``

Requires: ``DISCORD_BOT_TOKEN``, ``DISCORD_GATEWAY_ENABLED``
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord_notifier

logger = logging.getLogger(__name__)


async def on_conversation_created(
    conversation_id: int,
    guild_slug: str,
    name: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Create a Discord thread mirroring a newly created Conversation.

    Creates a Discord thread in the guild's configured channel and stamps
    ``Conversation.discord_thread_id`` with the result — the field
    ``discord/router.py``'s inbound resolution and
    ``discord_notifier.notify_foreman_chat`` both read directly. Returns the
    Discord thread ID, or None if creation failed or Discord is not
    configured.

    This is the ONLY path that creates Discord threads for conversations.
    """
    if not discord_notifier.is_configured():
        return None

    channel = await discord_notifier._resolve_channel_for_guild(guild_slug)
    if not channel:
        logger.debug(
            "thread_mirror: no channel for guild=%s, skipping Discord thread creation",
            guild_slug,
        )
        return None

    thread_name = name or "💬 Conversation"
    # Prefix to distinguish from issue/task threads
    if not thread_name.startswith("💬"):
        thread_name = f"💬 {thread_name}"

    discord_thread_id = await discord_notifier._create_thread_in_channel(channel, thread_name[:100])
    if not discord_thread_id:
        logger.warning(
            "thread_mirror: failed to create Discord thread for conversation=%s",
            conversation_id,
        )
        return None

    await _stamp_discord_thread_id(conversation_id, discord_thread_id)

    logger.info(
        "thread_mirror: created Discord thread %s for conversation=%s",
        discord_thread_id,
        conversation_id,
    )
    return discord_thread_id


async def _stamp_discord_thread_id(conversation_id: int, discord_thread_id: str) -> None:
    """Write ``discord_thread_id`` onto the Conversation row.

    This is the only write from Discord-side back onto the Foreman model —
    it records WHERE the mirror lives, not any lifecycle state.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from foreman.conversation_service import broadcast_conversation_updated  # noqa: PLC0415
        from models import Conversation  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, conversation_id)
            if conversation is None:
                return
            conversation.discord_thread_id = discord_thread_id
            conversation.updated_at = datetime.now(UTC)
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
            # So the frontend's conversation panel picks up the freshly-bound
            # discord_thread_id live instead of showing "discord thread not
            # yet created" until the next manual reload (#1314).
            await broadcast_conversation_updated(db, conversation)
    except Exception:
        logger.warning(
            "thread_mirror: failed to stamp discord_thread_id on conversation=%s",
            conversation_id,
            exc_info=True,
        )


async def on_thread_updated(
    discord_thread_id: str | None,
    status: str | None = None,
    deleted_at: str | None = None,
) -> None:
    """Mirror a Foreman conversation status change to its Discord thread.

    Called with the Discord thread id the caller already has in hand
    (``Conversation.discord_thread_id``). Archives the Discord thread when Foreman
    archives/closes it, and un-archives when Foreman re-activates it. No-op
    if *discord_thread_id* is None (the conversation was never mirrored to
    Discord).
    """
    if not discord_notifier.is_configured():
        return
    if not discord_thread_id:
        return

    if status == "archived" or status == "closed" or deleted_at:
        await discord_notifier._bot_request(
            "patch",
            f"/channels/{discord_thread_id}",
            {"archived": True},
        )
        logger.debug(
            "thread_mirror: archived Discord thread %s (foreman status=%s)",
            discord_thread_id,
            status,
        )
    elif status == "active":
        # Un-archive: set archived=False so the thread is visible again
        await discord_notifier._bot_request(
            "patch",
            f"/channels/{discord_thread_id}",
            {"archived": False},
        )
        logger.debug(
            "thread_mirror: un-archived Discord thread %s (foreman status=active)",
            discord_thread_id,
        )


async def relay_discord_thread_event(
    discord_thread_id: str,
    discord_status: str,
    *,
    soft_delete: bool = False,
) -> None:
    """Relay a Discord-side thread event inward without treating it as authoritative.

    Replaces the old ``_sync_thread_status`` which wrote Discord state directly
    onto the Foreman model as if Discord owned the lifecycle. Instead:

    - If a Discord thread is archived/deleted, we log it for observability
      but do NOT change the Conversation.status — the Foreman decides when
      a thread is done (via ``conversation_maintenance.py``).
    - If a user posts in an archived Discord thread (handled in router.py),
      that message is forwarded to the Foreman as normal input. If the Foreman
      decides the thread should be re-activated, it will update the Conversation
      status and this mirror will un-archive the Discord thread in response
      (via ``on_thread_updated``).

    This function exists for observability and future extensibility (e.g.
    notifying the Foreman that a user manually archived a thread).
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Conversation  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Conversation).where(col(Conversation.discord_thread_id) == discord_thread_id)
            )
            conversation = result.first()
            if conversation is None:
                # Not a Foreman-managed thread; ignore
                return

            logger.info(
                "thread_mirror: Discord thread %s event=%s (conversation=%s status=%s) "
                "— NOT changing Foreman state (Discord is not authoritative)",
                discord_thread_id,
                discord_status,
                conversation.id,
                conversation.status,
            )
    except Exception:
        logger.warning(
            "thread_mirror: relay_discord_thread_event failed discord_thread=%s",
            discord_thread_id,
            exc_info=True,
        )


async def rename_conversation_thread(discord_thread_id: str, name: str) -> None:
    """Rename the Discord thread bound to a Conversation (issue #1278).

    Called by ``foreman.conversation_service.rename_conversation`` directly
    with ``Conversation.discord_thread_id`` — the source of truth for a
    conversation's Discord binding
    here, same as ``on_thread_updated``. No-op if Discord isn't
    configured. Never raises (``discord_notifier.rename_thread`` never
    raises).
    """
    if not discord_notifier.is_configured():
        return
    await discord_notifier.rename_thread(discord_thread_id, name)


async def archive_conversation_thread_by_id(discord_thread_id: str) -> None:
    """Archive the Discord thread bound to a Conversation (issue #1278).

    Called by ``foreman.conversation_service.close_conversation`` directly
    with ``Conversation.discord_thread_id``. No-op if Discord isn't configured. Never
    raises.
    """
    if not discord_notifier.is_configured():
        return
    await discord_notifier.archive_thread(discord_thread_id)
