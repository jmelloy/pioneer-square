"""Inbound Discord chat routing to Foreman (Phase 5): the consumer side of
the Gateway websocket (#743/#744) — decides which Foreman session a chat
message belongs to, forwards it, and lets the existing Foreman chat mirror
(``discord_notifier.notify_foreman_chat``) post the reply back into the same
thread.

Thread routing model (#885 — unified via the single ``discord_thread_bindings``
table, keyed on ``(subject_type, subject_key)``):
    - a message in an ``"issue"`` thread (a per-PR/issue thread) is chat about
      whichever task is linked to that issue/PR — routed with ``task_id`` set,
      so the reply lands back in the same thread (see
      ``discord_notifier.notify_foreman_chat``).
    - a message in a ``"task_stream"`` thread (a task's live terminal-output
      feed) is chat about that task directly — routed with ``task_id`` set to
      the task the thread streams for. Before this table unification, these
      threads carried no inbound binding at all, so a reply here was silently
      dropped before ever reaching this router (see
      ``discord/gateway.py:_query_channel_wired``, which now also covers them).
    - a message in a ``"foreman_daily"`` thread (a legacy, no-longer-created
      dated Foreman thread — kept only so previously existing threads keep
      routing), or in any other wired channel with no thread binding, is
      general/ad-hoc chat for that Pioneer Square guild — routed with
      ``task_id=None``, so the reply is posted directly to the guild's main
      configured channel. ``notify_foreman_chat`` never creates a new dated
      thread; that fallback has been removed.
    - one exception to the above: ``_MENTION_ONLY_CHANNEL_ID`` (#962) is a
      wired channel that only forwards a message when it explicitly
      @mentions the bot — every other message posted there is ignored. The
      mention token is stripped before the content is forwarded as ad-hoc
      chat (``task_id=None``), same as any other wired channel.
    - a second, higher-priority exception: a message that @-mentions the
      foreman role (``discord.auth.mentions_foreman_role``) is routed by
      *author*, not by channel — see ``_route_foreman_mention``. It is
      checked first, before any of the channel-scoped logic above, so it
      always gets a response in any channel or DM the Gateway forwards it
      from (see ``discord/gateway.py``), independent of whether that channel
      is wired to any particular guild.
    - anything else has nowhere to route to and is silently ignored.

A reply in a thread already bound to a task (``task_id`` resolved above) is
auto-routed (#959) straight to the task-mutating tool, bypassing the Foreman
AI entirely — see ``_auto_route_task_reply``:
    - task ``state == "working"`` → ``redirect_task`` (course-correct the
      running worker in place).
    - task ``state`` in ``awaiting-review``/``done``/``parked`` → ``send_followup``
      (continue on the same branch).
    - any other state (e.g. ``pending``, or the task can't be found) falls
      back to the normal Foreman-chat path below, tagged
      ``[discord-thread-reply]`` as before.

Consumes ``discord.gateway.gateway_message_queue``. Enable with the same
env vars as the Gateway (``DISCORD_GATEWAY_ENABLED`` + ``DISCORD_BOT_TOKEN``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from types import SimpleNamespace

from discord.auth import is_member_authorized, mentions_foreman_role, strip_foreman_role_mention
from discord.gateway import gateway_message_queue

logger = logging.getLogger(__name__)

# #962 — see the routing-model exception in the module docstring above.
# Override via env for testing/ops without a code change.
_MENTION_ONLY_CHANNEL_ID = os.environ.get("DISCORD_MENTION_CHANNEL_ID") or "1528707144227225631"

# Task states routed to send_followup by _auto_route_task_reply (#959) — the
# work is paused and needs a new iteration on the same branch. "working" is
# handled separately (redirect_task, course-correcting the live worker); any
# other state (pending, failed, cancelled, ...) falls back to normal Foreman
# chat routing.
_FOLLOWUP_STATES = frozenset({"awaiting-review", "done", "parked"})


def _bot_user_id() -> str | None:
    return os.environ.get("DISCORD_APPLICATION_ID") or None


def _is_bot_mentioned(message: dict, bot_id: str) -> bool:
    """Return True if *bot_id* appears in the message's ``mentions`` array."""
    mentions = message.get("mentions") or []
    return any(str(user.get("id")) == bot_id for user in mentions if isinstance(user, dict))


def _strip_mention(content: str, bot_id: str) -> str:
    """Remove every ``<@bot_id>``/``<@!bot_id>`` mention token from *content*."""
    pattern = re.compile(rf"<@!?{re.escape(bot_id)}>")
    return pattern.sub("", content).strip()


async def _lookup_binding(thread_id: str) -> tuple[str, str] | None:
    """Return ``(subject_type, subject_key)`` for a bound Discord thread, or None."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordThreadBinding  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordThreadBinding.subject_type, DiscordThreadBinding.subject_key).where(
                    col(DiscordThreadBinding.thread_id) == thread_id
                )
            )
            return result.first()
    except Exception:
        logger.warning(
            "discord router: thread binding lookup failed thread=%s", thread_id, exc_info=True
        )
        return None


async def _resolve_issue_session(subject_key: str) -> tuple[str, str] | None:
    """Return ``(ps_guild_slug, task_id)`` for an ``"issue"`` subject_key ``"repo#number"``.

    Picks the most recently created task for that (issue_repo, issue_number)
    pair when more than one exists (e.g. an execute task and a follow-up
    review task both linked to the same PR). Never raises.
    """
    repo, _, number_str = subject_key.rpartition("#")
    if not repo or not number_str.isdigit():
        return None
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Guild, Task  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Task.id, Guild.slug)
                .join(Guild, col(Guild.id) == col(Task.guild_id))
                .where(
                    col(Task.issue_repo) == repo,
                    col(Task.issue_number) == int(number_str),
                )
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
            "discord router: issue session lookup failed key=%s", subject_key, exc_info=True
        )
        return None


async def _resolve_task_stream_session(task_id: str) -> tuple[str, str] | None:
    """Return ``(ps_guild_slug, task_id)`` for a ``"task_stream"`` subject_key (a bare task_id).

    No issue/PR linkage required — a task's stream thread exists from the
    moment the task starts running, long before (if ever) it gains a PR.
    Never raises.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Guild, Task  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Guild.slug)
                .join(Task, col(Task.guild_id) == col(Guild.id))
                .where(col(Task.id) == task_id)
            )
            slug = result.first()
            return (slug, task_id) if slug else None
    except Exception:
        logger.warning(
            "discord router: task-stream session lookup failed task=%s", task_id, exc_info=True
        )
        return None


def _resolve_foreman_daily_session(subject_key: str) -> str | None:
    """Return the guild slug encoded in a ``"foreman_daily"`` subject_key ``"slug:session_key"``."""
    slug, _, _session_key = subject_key.partition(":")
    return slug or None


async def _resolve_channel_guild(channel_id: str) -> str | None:
    """Return the Pioneer Square guild slug bound to *channel_id* via ``/join-channel``.

    Used for messages posted directly in a wired channel, outside of any
    thread. Never raises.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordChannelGuild  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordChannelGuild.ps_guild_id).where(
                    col(DiscordChannelGuild.discord_channel_id) == channel_id
                )
            )
            return result.first()
    except Exception:
        logger.warning(
            "discord router: channel binding lookup failed channel=%s", channel_id, exc_info=True
        )
        return None


async def _guild_pk_for_slug(guild_slug: str) -> int | None:
    """Return the integer ``guilds.id`` for *guild_slug*, or None. Never raises."""
    try:
        from auth_deps import get_guild_pk  # noqa: PLC0415
        from database import AsyncSessionLocal  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            return await get_guild_pk(db, guild_slug)
    except Exception:
        logger.warning("discord router: guild pk lookup failed slug=%s", guild_slug, exc_info=True)
        return None


async def resolve_session(channel_id: str) -> tuple[str, str | None] | None:
    """Return ``(ps_guild_slug, task_id)`` for *channel_id*, or None if unresolvable.

    ``task_id`` is None for general/ad-hoc chat. Looks up the single
    ``discord_thread_bindings`` row for *channel_id* and dispatches on its
    ``subject_type`` — see the routing model in the module docstring. Falls
    back to the plain ``/join-channel`` binding when *channel_id* isn't a
    known thread at all.
    """
    binding = await _lookup_binding(channel_id)
    if binding:
        subject_type, subject_key = binding
        if subject_type == "issue":
            session = await _resolve_issue_session(subject_key)
            if session:
                return session
        elif subject_type == "task_stream":
            session = await _resolve_task_stream_session(subject_key)
            if session:
                return session
        elif subject_type == "foreman_daily":
            slug = _resolve_foreman_daily_session(subject_key)
            if slug:
                return (slug, None)

    guild_slug = await _resolve_channel_guild(channel_id)
    return (guild_slug, None) if guild_slug else None


async def _resolve_identity(
    discord_user_id: str | None,
    username: str | None,
    *,
    is_bot: bool = False,
    guild_pk: int | None = None,
) -> tuple[str | None, str]:
    """Return ``(ps_user_id, label)`` for a Discord author.

    Resolves through the ``/connect-account`` link, the single source of
    truth for Discord -> Pioneer Square identity. Falls back to
    ``(None, "a Discord user")`` — or the Discord username, if known — when
    the author has not linked an account, *unless* ``is_bot`` is set: an
    unlinked bot author is auto-provisioned its own ``users`` row (see
    ``discord.bot_users.ensure_bot_user``) so its actions get an audit trail
    and inherited guild permissions instead of resolving to nobody. Never
    raises.
    """
    generic = f"Discord user @{username}" if username else "a Discord user"
    if not discord_user_id:
        return None, generic

    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordAccountLink, User  # noqa: PLC0415
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
    except Exception:
        logger.warning(
            "discord router: identity lookup failed discord_user=%s", discord_user_id, exc_info=True
        )
        return None, generic

    if is_bot:
        try:
            from discord.bot_users import ensure_bot_user  # noqa: PLC0415

            ps_user_id = await ensure_bot_user(discord_user_id, username, guild_pk)
            return ps_user_id, f"🤖 {username}" if username else f"🤖 {discord_user_id}"
        except Exception:
            logger.warning(
                "discord router: bot auto-provisioning failed discord_user=%s",
                discord_user_id,
                exc_info=True,
            )

    return None, generic


def _is_authorized(message: dict) -> bool:
    return is_member_authorized(message.get("member"))


async def _task_state_row(task_id: str) -> tuple[str, str | None, str | None] | None:
    """Return ``(state, phase, worker_id)`` for *task_id*, or None if not found. Never raises."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Task  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(col(Task.state), col(Task.phase), col(Task.worker_id)).where(
                    col(Task.id) == task_id
                )
            )
            return result.first()
    except Exception:
        logger.warning("discord router: task state lookup failed task=%s", task_id, exc_info=True)
        return None


async def _auto_route_task_reply(
    guild_slug: str, task_id: str, instructions: str, *, user_id: str | None
) -> bool:
    """Route a task-bound thread reply straight to redirect_task/send_followup (#959),
    bypassing the Foreman AI — see the module docstring.

    Returns True once the reply has been routed this way (the underlying tool call's
    own success/failure is logged by ``exec_tools``/``_exec_one_tool`` regardless), or
    False if *task_id* can't be found or its state isn't one this auto-routes, so the
    caller should fall back to the normal Foreman-chat path.
    """
    row = await _task_state_row(task_id)
    if row is None:
        return False
    state, phase, worker_id = row

    if state == "working":
        tool_name = "redirect_task"
    elif state in _FOLLOWUP_STATES:
        tool_name = "send_followup"
    else:
        return False

    from foreman.tools import exec_tools  # noqa: PLC0415

    tool_use = SimpleNamespace(
        id=f"discord-auto-route:{task_id}",
        name=tool_name,
        input={"task_id": task_id, "instructions": instructions},
    )
    results = await exec_tools(guild_slug, [tool_use], user_id=user_id)
    result = results[0] if results else {}
    logger.info(
        "discord router: auto-routed thread reply task=%s state=%s phase=%s worker=%s "
        "action=%s error=%s result=%r",
        task_id,
        state,
        phase,
        worker_id,
        tool_name,
        result.get("is_error", False),
        result.get("content"),
    )
    return True


async def _resolve_user_guild_slugs(ps_user_id: str) -> list[str]:
    """Return every Pioneer Square guild slug *ps_user_id* is a member of.

    Ordered most-recently-created first — the closest available proxy for
    "most relevant," since there is no per-user "last active project" field
    to prefer instead (see ``routes/guilds.py:list_guilds``, which orders the
    same way for the same reason). Never raises.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Guild, GuildMember  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Guild.slug)
                .join(GuildMember, col(GuildMember.guild_id) == col(Guild.id))
                .where(col(GuildMember.user_id) == ps_user_id)
                .order_by(col(Guild.created_at).desc())
            )
            return [slug for slug in result.all() if slug]
    except Exception:
        logger.warning(
            "discord router: accessible-guilds lookup failed user=%s", ps_user_id, exc_info=True
        )
        return []


async def _route_foreman_mention(message: dict, content: str) -> None:
    """Route a foreman-role @-mention to the Foreman, scoped by *author*
    rather than by the channel the message landed in.

    Every other inbound path resolves a Foreman session from the *channel*
    a message landed in (see ``resolve_session``) — this path ignores that
    entirely, so a foreman-role mention always gets a response in any
    channel or DM the bot can see, regardless of which (if any) guild
    project that channel happens to be wired to. The author still has to be
    role-authorized (``_is_authorized``) and resolve to a linked Pioneer
    Square user (``_resolve_identity``) with at least one accessible
    project (``_resolve_user_guild_slugs``) — an author that fails any of
    those checks has nowhere well-defined to route to and is silently
    ignored, same as an unresolvable channel elsewhere in this module.

    When the author has more than one accessible project, the most recently
    created one is used as the trigger target (a single Foreman trigger is
    inherently scoped to one project — see ``_trigger_foreman``), with the
    rest listed in the forwarded message so the Foreman can answer with
    context spanning all of the author's projects rather than just the one
    it's replying from. Never raises.
    """
    if not content:
        return
    if not _is_authorized(message):
        logger.info("discord router: unauthorized author — ignoring foreman-role mention")
        return

    author = message.get("author") or {}
    ps_user_id, label = await _resolve_identity(author.get("id"), author.get("username"))
    if not ps_user_id:
        logger.info(
            "discord router: foreman-role mention from unlinked discord_user=%s — ignoring",
            author.get("id"),
        )
        return

    guild_slugs = await _resolve_user_guild_slugs(ps_user_id)
    if not guild_slugs:
        logger.info(
            "discord router: foreman-role mention from user=%s with no accessible projects "
            "— ignoring",
            ps_user_id,
        )
        return

    guild_slug, *other_projects = guild_slugs
    context_note = f" (also has access to: {', '.join(other_projects)})" if other_projects else ""
    human_message = f"[discord-foreman-mention] project={guild_slug}{context_note}\n[Discord] {label}: {content}"

    try:
        await _persist_inbound_message(guild_slug, content, user_id=ps_user_id, task_id=None)
    except Exception:
        logger.warning(
            "discord router: failed to persist foreman-role mention guild=%s — forwarding to "
            "Foreman anyway",
            guild_slug,
            exc_info=True,
        )

    from foreman.runner import reset_foreman_poll  # noqa: PLC0415
    from ws_handlers import _trigger_foreman  # noqa: PLC0415

    await _trigger_foreman(
        guild_slug,
        "chat",
        human_message,
        user_id=ps_user_id,
        task_id=None,
        task_name=f"foreman.discord-mention:{guild_slug}",
    )
    reset_foreman_poll(guild_slug)


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

    if mentions_foreman_role(message):
        await _route_foreman_mention(message, strip_foreman_role_mention(content))
        return

    if channel_id == _MENTION_ONLY_CHANNEL_ID:
        bot_id = _bot_user_id()
        if not bot_id or not _is_bot_mentioned(message, bot_id):
            logger.debug(
                "discord router: channel=%s only responds to @mentions — ignoring", channel_id
            )
            return
        content = _strip_mention(content, bot_id)
        if not content:
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
    guild_pk = await _guild_pk_for_slug(guild_slug)
    ps_user_id, label = await _resolve_identity(
        author.get("id"),
        author.get("username"),
        is_bot=bool(author.get("bot")),
        guild_pk=guild_pk,
    )
    discord_line = f"[Discord] {label}: {content}"
    # A resolved task_id means this message landed in a thread already bound to a task
    # (an "issue" or "task_stream" subject binding) — tag it so the Foreman can route
    # straight to redirect_task/send_followup without a lookup, mirroring [github-event].
    human_message = (
        f"[discord-thread-reply] task_id={task_id}\n{discord_line}" if task_id else discord_line
    )

    try:
        await _persist_inbound_message(guild_slug, content, user_id=ps_user_id, task_id=task_id)
    except Exception:
        logger.warning(
            "discord router: failed to persist inbound message guild=%s — forwarding to "
            "Foreman anyway",
            guild_slug,
            exc_info=True,
        )

    if task_id and await _auto_route_task_reply(guild_slug, task_id, content, user_id=ps_user_id):
        return

    from foreman.runner import reset_foreman_poll  # noqa: PLC0415
    from ws_handlers import _trigger_foreman  # noqa: PLC0415

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
