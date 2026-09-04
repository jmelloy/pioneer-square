"""Discord thread mirroring of Foreman-owned threads (issue #1168).

This module is the single integration point between the Foreman's
thread lifecycle and Discord's thread representation. It subscribes to
Foreman thread events (``thread-created``, ``thread-updated``) and
mirrors them into Discord, instead of the Discord bot ever creating
threads independently.

Architecture:
    - Foreman creates/manages threads via ``foreman/thread_service.py``
    - This module observes those state changes and creates/updates Discord
      threads to match
    - Human messages in Discord threads are forwarded inward to the Foreman
      (via ``discord/router.py``) — this module does NOT handle inbound
    - Discord holds NO independent thread state: ``Thread.discord_thread_id``
      on the Foreman model is the only link, and it's written here after
      Discord confirms thread creation

Entry points:
    ``on_thread_created``  — called when Foreman creates a new thread
    ``on_thread_updated``  — called when Foreman updates thread status
    ``mirror_foreman_message`` — posts a Foreman reply into the mirrored Discord thread
    ``relay_discord_thread_event`` — replaces ``_sync_thread_status``: relays
        Discord-side archive/delete events inward without treating them as
        authoritative state changes

Requires: ``DISCORD_BOT_TOKEN``, ``DISCORD_GATEWAY_ENABLED``
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord_notifier

logger = logging.getLogger(__name__)


async def on_thread_created(
    thread_id: str,
    conversation_id: int,
    guild_slug: str,
    name: str | None = None,
    user_id: str | None = None,
) -> str | None:
    """Create a Discord thread mirroring a Foreman-created thread.

    Called when the Foreman creates a new thread (``thread-created`` event).
    Creates a Discord thread in the guild's configured channel and stamps
    ``Thread.discord_thread_id`` with the result. Returns the Discord thread
    ID, or None if creation failed or Discord is not configured.

    This is the ONLY path that creates Discord threads for conversations —
    never ``discord_notifier._ensure_conversation_thread`` independently.
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
            "thread_mirror: failed to create Discord thread for foreman thread=%s",
            thread_id,
        )
        return None

    # Stamp the discord_thread_id back onto the Foreman Thread row
    await _stamp_discord_thread_id(thread_id, discord_thread_id)

    # Also save a DiscordThreadBinding so inbound routing (discord/router.py)
    # can resolve messages in this Discord thread back to the conversation.
    # subject_type="conversation" with key="<thread_id>" maps cleanly.
    await discord_notifier._save_thread("conversation", thread_id, discord_thread_id)

    logger.info(
        "thread_mirror: created Discord thread %s for foreman thread=%s",
        discord_thread_id,
        thread_id,
    )
    return discord_thread_id


async def _stamp_discord_thread_id(thread_id: str, discord_thread_id: str) -> None:
    """Write ``discord_thread_id`` onto the Foreman's Thread row.

    This is the only write from Discord-side back onto the Foreman model —
    it records WHERE the mirror lives, not any lifecycle state.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from foreman.thread_service import sync_conversation_after_thread_update  # noqa: PLC0415
        from models import Thread  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Thread).where(
                    col(Thread.id) == thread_id,
                    col(Thread.deleted_at).is_(None),
                )
            )
            thread = result.first()
            if thread is None:
                return
            thread.discord_thread_id = discord_thread_id
            thread.updated_at = datetime.now(UTC)
            db.add(thread)
            await sync_conversation_after_thread_update(db, thread, previous_status=thread.status)
            await db.commit()
    except Exception:
        logger.warning(
            "thread_mirror: failed to stamp discord_thread_id on thread=%s",
            thread_id,
            exc_info=True,
        )


async def on_thread_updated(
    thread_id: str,
    status: str | None = None,
    deleted_at: str | None = None,
) -> None:
    """Mirror a Foreman thread status change to its Discord thread.

    Called when the Foreman changes thread status (``thread-updated`` event).
    Archives the Discord thread when Foreman archives/closes it, and
    un-archives when Foreman re-activates it.
    """
    if not discord_notifier.is_configured():
        return

    discord_thread_id = await _get_discord_thread_id(thread_id)
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


async def mirror_foreman_message(
    thread_id: str,
    content: str,
    *,
    guild_slug: str | None = None,
) -> None:
    """Post a Foreman reply into the mirrored Discord thread.

    This is the mirror-side equivalent of ``notify_foreman_chat`` for
    Foreman-owned threads: when the Foreman replies within a thread context,
    this posts the reply to the corresponding Discord thread.

    Falls back silently if no Discord thread exists for this Foreman thread.
    """
    if not content or not content.strip():
        return
    if not discord_notifier.is_configured():
        return

    discord_thread_id = await _get_discord_thread_id(thread_id)
    if not discord_thread_id:
        return

    # Truncate to Discord's message limit
    await discord_notifier._bot_request(
        "post",
        f"/channels/{discord_thread_id}/messages",
        {"content": content[:2000]},
    )


async def relay_discord_thread_event(
    discord_thread_id: str,
    discord_status: str,
    *,
    soft_delete: bool = False,
) -> None:
    """Relay a Discord-side thread event inward without treating it as authoritative.

    Replaces the old ``_sync_thread_status`` which wrote Discord state directly
    onto the Foreman Thread row as if Discord owned the lifecycle. Instead:

    - If a Discord thread is archived/deleted, we log it for observability
      but do NOT change the Foreman Thread.status — the Foreman decides when
      a thread is done (via ``thread_maintenance.py``).
    - If a user posts in an archived Discord thread (handled in router.py),
      that message is forwarded to the Foreman as normal input. If the Foreman
      decides the thread should be re-activated, it will update the Thread
      status and this mirror will un-archive the Discord thread in response
      (via ``on_thread_updated``).

    This function exists for observability and future extensibility (e.g.
    notifying the Foreman that a user manually archived a thread).
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Thread  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Thread).where(
                    col(Thread.discord_thread_id) == discord_thread_id,
                    col(Thread.deleted_at).is_(None),
                )
            )
            thread = result.first()
            if thread is None:
                # Not a Foreman-managed thread; ignore
                return

            logger.info(
                "thread_mirror: Discord thread %s event=%s (foreman thread=%s status=%s) "
                "— NOT changing Foreman state (Discord is not authoritative)",
                discord_thread_id,
                discord_status,
                thread.id,
                thread.status,
            )
    except Exception:
        logger.warning(
            "thread_mirror: relay_discord_thread_event failed discord_thread=%s",
            discord_thread_id,
            exc_info=True,
        )


async def _get_discord_thread_id(thread_id: str) -> str | None:
    """Look up the Discord thread ID for a Foreman thread."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Thread  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Thread.discord_thread_id).where(
                    col(Thread.id) == thread_id,
                    col(Thread.deleted_at).is_(None),
                )
            )
            return result.first()
    except Exception:
        logger.warning(
            "thread_mirror: failed to look up discord_thread_id for thread=%s",
            thread_id,
            exc_info=True,
        )
        return None
