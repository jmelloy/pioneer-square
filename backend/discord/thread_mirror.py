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
    ``on_thread_created``  — called when Foreman creates a new thread; the
        only place that stamps ``Thread.discord_thread_id`` (and, through it,
        ``Conversation.discord_thread_id`` — see ``_stamp_discord_thread_id``)
    ``on_thread_updated``  — called when Foreman changes a thread's status,
        given the Discord thread id directly by the caller (which already
        holds the ``Thread``/``Conversation`` row) — no id lookup here
    ``relay_discord_thread_event`` — replaces the deleted ``_sync_thread_status``:
        relays Discord-side archive/delete events inward without treating
        them as authoritative state changes
    ``rename_conversation_thread`` / ``archive_conversation_thread_by_id`` —
        issue #1278: mirror a *Conversation*-driven rename/close straight from
        ``Conversation.discord_thread_id`` (the source of truth for a
        conversation's Discord binding), with no ``Thread`` id lookup needed —
        used by ``foreman.conversation_service.rename_conversation``/
        ``close_conversation``.

Aside from ``on_thread_created`` (which stamps the mirror id back onto
``Thread``/``Conversation`` right after Discord confirms creation), every
other entry point here is keyed by a Discord thread id the caller already
has — never by looking a ``Thread`` id up itself (issue #1288).

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
    ``Thread.discord_thread_id`` with the result — which
    ``_stamp_discord_thread_id`` mirrors onto ``Conversation.discord_thread_id``
    too, the field ``discord/router.py``'s inbound resolution and
    ``discord_notifier.notify_foreman_chat`` both read directly. Returns the
    Discord thread ID, or None if creation failed or Discord is not
    configured.

    This is the ONLY path that creates Discord threads for conversations —
    this module never creates one independently of a Foreman thread-lifecycle
    event (issue #1288 removed the last fallback that once did,
    ``discord_notifier._ensure_conversation_thread``).
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

    # Also save a DiscordThreadBinding as a compatibility fallback for inbound
    # routing (discord/router.py) — the primary lookup there is
    # Conversation.discord_thread_id (just stamped above), but this binding
    # keeps a reply routable even after a newer Thread supersedes this one as
    # the conversation's active thread (see _resolve_foreman_thread_session).
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
    discord_thread_id: str | None,
    status: str | None = None,
    deleted_at: str | None = None,
) -> None:
    """Mirror a Foreman thread status change to its Discord thread.

    Called when the Foreman changes thread status (``thread-updated`` event),
    with the Discord thread id the caller already has in hand (its own
    ``Thread.discord_thread_id`` field) — this does no ``Thread`` lookup of
    its own (issue #1288). Archives the Discord thread when Foreman
    archives/closes it, and un-archives when Foreman re-activates it. No-op
    if *discord_thread_id* is None (the Foreman thread was never mirrored to
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


async def rename_conversation_thread(discord_thread_id: str, name: str) -> None:
    """Rename the Discord thread bound to a Conversation (issue #1278).

    Called by ``foreman.conversation_service.rename_conversation`` directly
    with ``Conversation.discord_thread_id`` — the source of truth for a
    conversation's Discord binding — so no ``Thread`` id lookup is needed
    here, same as ``on_thread_updated`` (issue #1288). No-op if Discord isn't
    configured. Never raises (``discord_notifier.rename_thread`` never
    raises).
    """
    if not discord_notifier.is_configured():
        return
    await discord_notifier.rename_thread(discord_thread_id, name)


async def archive_conversation_thread_by_id(discord_thread_id: str) -> None:
    """Archive the Discord thread bound to a Conversation (issue #1278).

    Called by ``foreman.conversation_service.close_conversation`` directly
    with ``Conversation.discord_thread_id`` — see
    ``rename_conversation_thread``'s docstring for why this skips the
    ``Thread``-id-keyed path. No-op if Discord isn't configured. Never
    raises.
    """
    if not discord_notifier.is_configured():
        return
    await discord_notifier.archive_thread(discord_thread_id)
