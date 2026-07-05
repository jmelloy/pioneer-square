"""Inbound Discord chat routing to Foreman (Phase 5): the consumer side of
the Gateway websocket (#743/#744) — decides which Foreman session a chat
message belongs to, forwards it, and lets the existing Foreman chat mirror
(``discord_notifier.notify_foreman_chat``) post the reply back into the same
thread.

Thread routing model:
    - a message in a thread mapped in ``discord_threads`` is chat about that
      task — routed with ``task_id`` set, so the reply lands in that task's
      thread (see ``discord_notifier.notify_foreman_chat``).
    - a message in a thread mapped in ``discord_foreman_threads`` (a legacy,
      no-longer-created dated Foreman thread — kept only so previously
      existing threads keep routing), or in any other wired channel with no
      task binding, is general/ad-hoc chat for that Pioneer Square guild —
      routed with ``task_id=None``, so the reply is posted directly to the
      guild's main configured channel. ``notify_foreman_chat`` never creates
      a new dated thread; that fallback has been removed.
    - anything else has nowhere to route to and is silently ignored.

Consumes ``discord.gateway.gateway_message_queue``. Enable with the same
env vars as the Gateway (``DISCORD_GATEWAY_ENABLED`` + ``DISCORD_BOT_TOKEN``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from discord.auth import is_member_authorized
from discord.gateway import gateway_message_queue

logger = logging.getLogger(__name__)


async def _resolve_task_session(thread_id: str) -> tuple[str, str] | None:
    """Return ``(ps_guild_slug, task_id)`` if *thread_id* is a known per-PR/issue thread.

    Picks the most recently created task for that (issue_repo, issue_number)
    pair when more than one exists (e.g. an execute task and a follow-up
    review task both linked to the same PR). A single joined query — rather
    than three sequential round-trips — since ``DiscordThread`` and ``Task``
    share the (issue_repo, issue_number) pair and ``Task.guild_id`` is a real
    FK into ``Guild``. Never raises.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordThread, Guild, Task  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Task.id, Guild.slug)
                .join(
                    DiscordThread,
                    (col(DiscordThread.issue_repo) == col(Task.issue_repo))
                    & (col(DiscordThread.issue_number) == col(Task.issue_number)),
                )
                .join(Guild, col(Guild.id) == col(Task.guild_id))
                .where(col(DiscordThread.thread_id) == thread_id)
                .order_by(col(Task.created_at).desc())
                .limit(1)
            )
            row = result.first()
            if not row:
                return None
            task_id, slug = row
            return (slug, task_id) if slug else None
    except Exception:
        logger.warning(
            "discord router: task session lookup failed thread=%s", thread_id, exc_info=True
        )
        return None


async def _resolve_general_session(channel_id: str) -> str | None:
    """Return the Pioneer Square guild slug for a general-chat *channel_id*.

    Checks the per-guild daily Foreman thread mapping first, then the plain
    ``/join-channel`` binding (for messages posted directly in a wired
    channel, outside of any thread). Never raises.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordChannelGuild, DiscordForemanThread  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordForemanThread.guild_id).where(
                    col(DiscordForemanThread.thread_id) == channel_id
                )
            )
            slug = result.first()
            if slug:
                return slug

            result = await db.exec(
                select(DiscordChannelGuild.ps_guild_id).where(
                    col(DiscordChannelGuild.discord_channel_id) == channel_id
                )
            )
            return result.first()
    except Exception:
        logger.warning(
            "discord router: general session lookup failed channel=%s", channel_id, exc_info=True
        )
        return None


async def resolve_session(channel_id: str) -> tuple[str, str | None] | None:
    """Return ``(ps_guild_slug, task_id)`` for *channel_id*, or None if unresolvable.

    ``task_id`` is None for general/ad-hoc chat. Checks task threads first,
    then general-chat threads/channels, per the routing model above.
    """
    task_session = await _resolve_task_session(channel_id)
    if task_session:
        return task_session
    guild_slug = await _resolve_general_session(channel_id)
    return (guild_slug, None) if guild_slug else None


async def _resolve_identity(
    discord_user_id: str | None, username: str | None
) -> tuple[str | None, str]:
    """Return ``(ps_user_id, label)`` for a Discord author.

    Tries the ``/connect-account`` link first (direct, authoritative), then
    the older ``discord_users`` mapping (also used by ``mention_or_login``,
    reversed here: Discord user ID -> GitHub login -> Pioneer Square user).
    Falls back to ``(None, "a Discord user")`` — or the Discord username, if
    known — when neither mapping exists. Never raises.
    """
    generic = f"Discord user @{username}" if username else "a Discord user"
    if not discord_user_id:
        return None, generic

    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordAccountLink, DiscordUser, User  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordAccountLink.ps_user_id).where(
                    col(DiscordAccountLink.discord_user_id) == discord_user_id
                )
            )
            ps_user_id = result.first()
            if ps_user_id:
                result = await db.exec(select(User.github_login).where(col(User.id) == ps_user_id))
                login = result.first()
                return ps_user_id, f"@{login}" if login else ps_user_id

            result = await db.exec(
                select(DiscordUser.github_login).where(
                    col(DiscordUser.discord_user_id) == discord_user_id
                )
            )
            login = result.first()
            if login:
                result = await db.exec(select(User.id).where(col(User.github_login) == login))
                ps_user_id = result.first()
                if ps_user_id:
                    return ps_user_id, f"@{login}"
    except Exception:
        logger.warning(
            "discord router: identity lookup failed discord_user=%s", discord_user_id, exc_info=True
        )

    return None, generic


def _is_authorized(message: dict) -> bool:
    return is_member_authorized(message.get("member"))


async def route_inbound_message(message: dict) -> bool:
    """Route one inbound Discord ``MESSAGE_CREATE`` payload to the Foreman AI.

    Never raises — this runs in a background consumer loop with no caller to
    propagate errors to. Returns True on success, False if routing failed
    (already logged), so the consumer loop can back off.
    """
    try:
        await _route_inbound_message(message)
        return True
    except Exception:
        logger.warning("discord router: failed to route inbound message", exc_info=True)
        return False


async def _route_inbound_message(message: dict) -> None:
    content = (message.get("content") or "").strip()
    if not content:
        return
    channel_id = message.get("channel_id")
    if not channel_id:
        return

    session = await resolve_session(channel_id)
    if session is None:
        logger.debug("discord router: no resolvable session for channel=%s — ignoring", channel_id)
        return
    guild_slug, task_id = session

    if not _is_authorized(message):
        logger.info(
            "discord router: unauthorized author — ignoring message for guild=%s", guild_slug
        )
        return

    author = message.get("author") or {}
    ps_user_id, label = await _resolve_identity(author.get("id"), author.get("username"))
    human_message = f"[Discord] {label}: {content}"

    try:
        await _persist_inbound_message(guild_slug, content, user_id=ps_user_id, task_id=task_id)
    except Exception:
        logger.warning(
            "discord router: failed to persist inbound message guild=%s — forwarding to "
            "Foreman anyway",
            guild_slug,
            exc_info=True,
        )

    from ws_handlers import _trigger_foreman  # noqa: PLC0415

    from foreman import reset_foreman_poll  # noqa: PLC0415

    await _trigger_foreman(
        guild_slug,
        "chat",
        human_message,
        user_id=ps_user_id,
        task_id=task_id,
        task_name=f"foreman.discord-chat:{guild_slug}",
    )
    reset_foreman_poll(guild_slug)


async def _persist_inbound_message(
    guild_slug: str, content: str, *, user_id: str | None, task_id: str | None
) -> None:
    """Write the inbound Discord message to the ``messages`` table and
    broadcast it over WS so the frontend chat panel shows it live, tagged
    ``source="discord"``. Best-effort — a failure here must not stop the
    message from reaching the Foreman, so the caller wraps this call in its
    own try/except and forwards to ``_trigger_foreman`` regardless.
    """
    from auth_deps import get_guild_pk  # noqa: PLC0415
    from database import AsyncSessionLocal  # noqa: PLC0415
    from events import broadcast_msg  # noqa: PLC0415
    from models import Message  # noqa: PLC0415
    from ws_types import ChatMsg  # noqa: PLC0415

    created_at = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        guild_pk = await get_guild_pk(db, guild_slug)
        if guild_pk is None:
            return
        db.add(
            Message(
                guild_id=guild_pk,
                from_agent="user",
                to_agent="foreman",
                content=content,
                message_type="chat",
                created_at=created_at,
                user_id=user_id,
                task_id=task_id,
                source="discord",
            )
        )
        await db.commit()

    await broadcast_msg(
        guild_slug,
        ChatMsg(
            from_="user",
            to="foreman",
            content=content,
            createdAt=created_at.isoformat(),
            userId=user_id,
            source="discord",
        ),
    )


async def _consume_forever(queue: asyncio.Queue) -> None:
    """Pull inbound messages off *queue* and route them, forever.

    Yields to the event loop after every message, and backs off briefly on a
    routing failure so a fast-failing broken message can't spin in a tight
    loop and starve other tasks.
    """
    while True:
        message = await queue.get()
        ok = await route_inbound_message(message)
        await asyncio.sleep(0 if ok else 1)


_router_task: asyncio.Task | None = None


def start_router() -> asyncio.Task | None:
    """Start the queue-consumer task, spawned via ``util.tasks.spawn``.

    No-op (returns None) unless the Gateway itself would start — there is
    nothing to consume otherwise. Safe to call more than once — returns the
    existing task if one is already running.
    """
    global _router_task
    from discord.gateway import _bot_token, _gateway_enabled  # noqa: PLC0415

    if not _gateway_enabled() or not _bot_token():
        return None
    if _router_task is not None and not _router_task.done():
        return _router_task

    from util.tasks import spawn  # noqa: PLC0415

    _router_task = spawn(_consume_forever(gateway_message_queue), name="discord-router")
    return _router_task


async def stop_router() -> None:
    """Cancel the running consumer task, if any, and wait for it to unwind."""
    global _router_task
    if _router_task is None:
        return
    _router_task.cancel()
    try:
        await _router_task
    except (asyncio.CancelledError, Exception):
        pass
    _router_task = None
