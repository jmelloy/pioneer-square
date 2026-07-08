"""Discord notification helpers, all driven by the bot (gateway app): flat-channel
embeds, per-PR/issue threads, Foreman chat threads + user mentions, and live
task-stream mirroring.

Everything requires the bot. Set ``DISCORD_BOT_TOKEN`` **and** ``DISCORD_CHANNEL_ID``
to post embeds to the configured channel and to create/reuse one Discord thread per
GitHub PR or issue. The thread mapping is persisted in the ``discord_threads`` DB table
so restarts never duplicate threads.

    When a thread-aware call has no ``(issue_repo, issue_number)`` — or thread
    creation fails — it falls back to a flat embed posted to the resolved channel
    via the bot. No bot token → silent no-op. HTTP and DB failures are always
    logged at WARNING level and never propagate.

Phase 3 — Foreman chat threads + user mentions
    ``notify_foreman_chat`` mirrors Foreman → user chat narration into Discord.

    When a chat line is scoped to a task (``task_id`` passed through) and that
    task's linked GitHub issue/PR already has a thread in ``discord_threads``,
    the line is posted there. In every other case — no task context, or a
    task with no linked thread yet — the line is posted directly to the
    guild's main configured Discord channel. There is no dated/daily
    fallback thread.

    ``mention_or_login`` resolves a GitHub login to a real ``<@id>`` Discord
    mention via the ``discord_users`` table (populated through the
    ``/api/discord-users`` REST endpoints). Falls back to a plain ``@login``
    string when no mapping exists — never raises, never blocks a notification.

Phase 4 — live task-stream mirroring
    ``notify_task_stream`` mirrors a worker task's streaming terminal output
    (Claude's assistant/thinking text) into a dedicated per-task Discord thread
    while the task is working. Lines are buffered and flushed in batches (see
    ``_STREAM_FLUSH_INTERVAL``) to stay under Discord's rate limit, and each
    batch is posted as a *silent* message (``SUPPRESS_NOTIFICATIONS`` flag, no
    mentions parsed) so a firehose of build output never pings anyone — a
    genuinely low-priority feed. Opt-in via ``DISCORD_STREAM_TASKS`` (requires a
    bot token, since the feed always routes into a thread — never a flat channel
    post). The per-task thread mapping persists in ``discord_task_threads``.

Phase 5 — new-member welcome DM
    ``send_welcome_dm`` is called by the Gateway client (``discord.gateway``)
    when a ``GUILD_MEMBER_ADD`` event arrives, and DMs the new member
    onboarding instructions pointing them at ``/connect-account``. Requires
    ``DISCORD_GATEWAY_ENABLED`` plus the privileged Server Members intent (see
    ``discord.gateway``). Message text is configurable via
    ``DISCORD_WELCOME_DM_TEXT``. DM failures (e.g. DMs disabled) are logged at
    WARNING and never raised.

Phase 6 — issue-rooted task tree thread routing
    Every thread-aware entry point (``notify_event``, ``notify_existing_thread``,
    ``notify_foreman_chat``) accepts a *task_id* and, before anything else,
    calls ``_resolve_root_thread_id`` to walk ``parent_task_id`` up to that
    task's root (the phase='issue' task created via ``create_task``). When the
    root has a ``discord_thread_id`` — set once, immediately, by
    ``ensure_issue_root_thread`` when the root task is minted — the
    notification posts there, so every plan/execute/review/followup task
    parented to the same issue lands in one stable thread for the issue's
    whole lifetime. Falls back to the legacy ``issue_repo``/``issue_number``
    routing below when *task_id* has no root, or the root predates this
    column and has no thread yet.

Required bot permissions: ``SEND_MESSAGES``, ``CREATE_PUBLIC_THREADS``, ``MANAGE_THREADS``.

Usage::

    # Flat embed posted to the configured channel via the bot:
    await discord_notifier.notify("task-complete", "Task done", "t-abc finished")

    # Thread-aware (routes to a per-PR thread or falls back to the channel):
    await discord_notifier.notify_event(
        "pr-opened",
        title="#42: Fix the bug",
        description="Opened by @alice",
        url="https://github.com/org/repo/pull/42",
        issue_repo="org/repo",
        issue_number=42,
        thread_name="#42: Fix the bug",
        header_fields={"Assignee": "@alice", "Labels": "bug"},
    )

    # Foreman chat mirrored into the main channel (or a per-task thread when
    # task_id resolves to one):
    await discord_notifier.notify_foreman_chat(
        guild_id, "Spun up worker w-abc for t-123.", task_id="t-123"
    )

    # Resolve a GitHub login to a Discord mention (or a plain @login fallback):
    mention = await discord_notifier.mention_or_login("alice")
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)

# Reused across all calls; closed on application exit (or left to GC).
_client: httpx.AsyncClient | None = None

_DISCORD_API_BASE = "https://discord.com/api/v10"

# Colour palette for Discord embed sidebar
_COLOURS: dict[str, int] = {
    "task-complete": 0x2ECC71,  # green
    "task-failed": 0xE74C3C,  # red
    "task-cancelled": 0xE74C3C,  # red
    "task-assigned": 0x1ABC9C,  # teal
    "task-followup": 0xF1C40F,  # yellow
    "task-redirect": 0xE67E22,  # orange
    "pr-opened": 0x3498DB,  # blue
    "pr-updated": 0x5DADE2,  # light blue
    "pr-review": 0xF39C12,  # gold
    "pr-merged": 0x9B59B6,  # purple
    "pr-closed": 0x95A5A6,  # grey
    "worker-online": 0x1ABC9C,  # teal
    "worker-offline": 0xE67E22,  # orange
    "ci-pass": 0x2ECC71,  # green
    "ci-fail": 0xE74C3C,  # red
}
_DEFAULT_COLOUR = 0x7289DA  # blurple


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    return _client


def _bot_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN") or None


def _channel_id() -> str | None:
    return os.environ.get("DISCORD_CHANNEL_ID") or None


def is_configured() -> bool:
    """Return True if the Discord bot (gateway app) is set up.

    Cheap, side-effect-free check callers can use to skip expensive prep work
    (e.g. a live GitHub API call to resolve a thread title) when Discord
    notifications are disabled entirely.
    """
    return bool(_bot_token())


# ---------------------------------------------------------------------------
# Flat-channel notification (posted via the bot)
# ---------------------------------------------------------------------------


async def notify(
    event_type: str,
    title: str,
    description: str,
    url: str | None = None,
    color: int | None = None,
    ps_guild_slug: str | None = None,
) -> None:
    """Post a Discord embed to the bot's configured channel.

    Resolves the destination channel via the ``discord_channel_guilds``
    binding for *ps_guild_slug* (when given), falling back to the flat
    ``DISCORD_CHANNEL_ID`` env var. Silent no-op when the bot token or a
    destination channel are not configured. Never raises — errors are logged
    at WARNING level by the underlying bot request.
    """
    if not _bot_token():
        return

    channel = await _resolve_channel_for_guild(ps_guild_slug)
    if not channel:
        return

    await _post_to_thread(channel, event_type, title, description, url=url, color=color)


# ---------------------------------------------------------------------------
# Bot-based thread helpers
# ---------------------------------------------------------------------------


async def _bot_request_raw(
    method: str,
    path: str,
    json_body: dict | None = None,
) -> dict:
    """Make an authenticated Discord bot API request, raising on failure.

    Returns the parsed JSON response dict, or an empty dict for 204/empty
    bodies. Unlike ``_bot_request``, this does not swallow errors — callers
    that need to distinguish an expected API failure (``httpx.HTTPError``)
    from a genuine bug use this directly (see ``send_welcome_dm``).
    """
    token = _bot_token()
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    client = _get_client()
    resp = await getattr(client, method)(
        f"{_DISCORD_API_BASE}{path}",
        json=json_body,
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


async def _bot_request(
    method: str,
    path: str,
    json_body: dict | None = None,
) -> dict | None:
    """Make an authenticated Discord bot API request.

    Returns the parsed JSON response dict, an empty dict for 204/empty bodies,
    or None on error.  Never raises.
    """
    if not _bot_token():
        return None
    try:
        return await _bot_request_raw(method, path, json_body)
    except Exception:
        logger.warning(
            "Discord bot API request failed method=%s path=%s", method, path, exc_info=True
        )
        return None


async def _resolve_channel_for_guild(ps_guild_slug: str | None) -> str | None:
    """Return the Discord channel wired to *ps_guild_slug* via ``/join-channel``.

    Falls back to the flat ``DISCORD_CHANNEL_ID`` env var when no binding
    exists in the ``discord_channel_guilds`` table (or *ps_guild_slug* is
    None). Never raises.
    """
    if ps_guild_slug:
        try:
            from database import AsyncSessionLocal  # noqa: PLC0415
            from models import DiscordChannelGuild  # noqa: PLC0415
            from sqlmodel import col, select  # noqa: PLC0415

            async with AsyncSessionLocal() as db:
                # Multiple channels can be bound to the same PS guild; order by
                # created_at so the first-registered channel always wins rather
                # than an arbitrary, backend-dependent row order.
                rows = (
                    await db.exec(
                        select(DiscordChannelGuild.discord_channel_id)
                        .where(col(DiscordChannelGuild.ps_guild_id) == ps_guild_slug)
                        .order_by(col(DiscordChannelGuild.created_at).asc())
                    )
                ).all()
                if rows:
                    return rows[0]
        except Exception:
            logger.warning(
                "discord: channel binding lookup failed guild=%s", ps_guild_slug, exc_info=True
            )
    return _channel_id()


async def _lookup_thread(issue_repo: str, issue_number: int) -> str | None:
    """Return the persisted Discord thread_id for a PR/issue, or None."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415 — lazy to avoid circular import
        from models import DiscordThread  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordThread).where(
                    col(DiscordThread.issue_repo) == issue_repo,
                    col(DiscordThread.issue_number) == issue_number,
                )
            )
            row = result.one_or_none()
            return row.thread_id if row else None
    except Exception:
        logger.warning(
            "discord: thread DB lookup failed repo=%s number=%s",
            issue_repo,
            issue_number,
            exc_info=True,
        )
        return None


async def _save_thread(issue_repo: str, issue_number: int, thread_id: str) -> None:
    """Persist a new Discord thread mapping using an atomic upsert (ignore duplicates)."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordThread  # noqa: PLC0415
        from sqlalchemy.dialects.postgresql import insert  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            stmt = (
                insert(DiscordThread)
                .values(
                    issue_repo=issue_repo,
                    issue_number=issue_number,
                    thread_id=thread_id,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing()
            )
            await db.execute(stmt)
            await db.commit()
    except Exception:
        logger.warning(
            "discord: thread DB save failed repo=%s number=%s thread=%s",
            issue_repo,
            issue_number,
            thread_id,
            exc_info=True,
        )


async def _create_thread_in_channel(channel: str, thread_name: str) -> str | None:
    """Create a new thread in *channel*, anchored by a starter message.

    For standard text channels, thread creation requires a starter message first:
    POST /channels/{channel}/messages → get message_id
    POST /channels/{channel}/messages/{message_id}/threads → get thread_id

    Returns the new thread_id, or None on API error.
    """
    starter = await _bot_request(
        "post",
        f"/channels/{channel}/messages",
        {"content": thread_name[:100]},
    )
    if not starter:
        return None
    message_id = starter.get("id")
    if not message_id:
        logger.warning("discord: starter message returned no id for channel=%s", channel)
        return None

    data = await _bot_request(
        "post",
        f"/channels/{channel}/messages/{message_id}/threads",
        {"name": thread_name[:100]},
    )
    if not data:
        return None

    new_thread_id = data.get("id")
    if not new_thread_id:
        logger.warning("discord: thread creation returned no id for channel=%s", channel)
        return None
    return new_thread_id


async def _ensure_thread(
    issue_repo: str, issue_number: int, thread_name: str, channel: str | None = None
) -> str | None:
    """Return the Discord thread_id for a PR/issue, creating it if necessary.

    *channel* overrides the flat ``DISCORD_CHANNEL_ID`` env var when provided
    (used to route to a per-guild channel bound via ``/join-channel``).

    Returns None when bot token or channel ID are not configured, or on API error.
    """
    existing = await _lookup_thread(issue_repo, issue_number)
    if existing:
        return existing

    channel = channel or _channel_id()
    if not channel:
        return None

    new_thread_id = await _create_thread_in_channel(channel, thread_name)
    if not new_thread_id:
        return None

    await _save_thread(issue_repo, issue_number, new_thread_id)
    # Re-fetch after save: on_conflict_do_nothing means a concurrent creator wins;
    # re-fetching ensures both callers use the same persisted thread_id.
    return await _lookup_thread(issue_repo, issue_number) or new_thread_id


# ---------------------------------------------------------------------------
# Issue-rooted task tree: stable thread anchored to the phase='issue' root task
# ---------------------------------------------------------------------------

# Guards _resolve_root_thread_id against an (expected-impossible) cyclic
# parent_task_id chain so a bad row can never spin the lookup forever.
_MAX_PARENT_CHAIN_DEPTH = 20


async def _resolve_root_thread_id(task_id: str) -> str | None:
    """Walk ``parent_task_id`` up from *task_id* to its root and return the root's thread.

    The root is the top of the chain (``parent_task_id IS NULL``), normally the
    phase='issue' task created via ``create_task``. Returns None when the
    chain has no root with a ``discord_thread_id`` set — callers should fall
    back to the legacy issue_repo/issue_number thread lookup (unrooted or
    pre-migration tasks). Never raises.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Task  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            current_id = task_id
            for _ in range(_MAX_PARENT_CHAIN_DEPTH):
                result = await db.exec(
                    select(Task.parent_task_id, Task.discord_thread_id).where(
                        col(Task.id) == current_id
                    )
                )
                row = result.one_or_none()
                if row is None:
                    return None
                parent_id, thread_id = row
                if parent_id is None:
                    return thread_id
                current_id = parent_id
            logger.warning(
                "discord: parent_task_id chain exceeded depth=%d starting task=%s",
                _MAX_PARENT_CHAIN_DEPTH,
                task_id,
            )
            return None
    except Exception:
        logger.warning("discord: root-thread resolution failed task=%s", task_id, exc_info=True)
        return None


async def _save_root_thread(task_id: str, thread_id: str) -> None:
    """Persist *thread_id* directly on the root task's ``discord_thread_id`` column."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Task  # noqa: PLC0415
        from sqlalchemy import update  # noqa: PLC0415
        from sqlmodel import col  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            await db.exec(
                update(Task).where(col(Task.id) == task_id).values(discord_thread_id=thread_id)
            )
            await db.commit()
    except Exception:
        logger.warning(
            "discord: root thread DB save failed task=%s thread=%s",
            task_id,
            thread_id,
            exc_info=True,
        )


async def ensure_issue_root_thread(
    task_id: str, thread_name: str, ps_guild_slug: str | None = None
) -> str | None:
    """Create and persist the stable Discord thread for a new issue-root task.

    Called once, when a phase='issue' root task is minted (see
    ``create_task`` in ``foreman/tools.py``) — not lazily on first child
    post — so every task parented to *task_id* via ``parent_task_id``
    resolves the same thread through ``_resolve_root_thread_id`` for the
    rest of the issue's lifetime. Returns the new thread_id, or None when
    Discord isn't configured or thread creation fails. Never raises.
    """
    if not _bot_token():
        return None
    channel = await _resolve_channel_for_guild(ps_guild_slug)
    if not channel:
        return None
    thread_id = await _create_thread_in_channel(channel, thread_name)
    if not thread_id:
        return None
    await _save_root_thread(task_id, thread_id)
    return thread_id


async def _post_to_thread(
    thread_id: str,
    event_type: str,
    title: str,
    description: str,
    url: str | None = None,
    color: int | None = None,
    fields: dict[str, str] | None = None,
) -> None:
    """Post an embed to an existing Discord thread. Never raises."""
    resolved_color = color if color is not None else _COLOURS.get(event_type, _DEFAULT_COLOUR)
    embed: dict = {"title": title, "description": description, "color": resolved_color}
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = [{"name": k, "value": v, "inline": True} for k, v in fields.items()]
    await _bot_request("post", f"/channels/{thread_id}/messages", {"embeds": [embed]})


async def archive_thread(thread_id: str) -> None:
    """Archive a Discord thread. Never raises."""
    await _bot_request("patch", f"/channels/{thread_id}", {"archived": True})


# ---------------------------------------------------------------------------
# New-member welcome DM
# ---------------------------------------------------------------------------

_DEFAULT_WELCOME_DM_TEXT = (
    "Welcome, {username}! 👋\n\n"
    "Pioneer Square is an AI-assisted engineering system: a Foreman AI turns GitHub "
    "issues into shipped pull requests using a fleet of coding workers.\n\n"
    "Run `/connect-account` here to link your Discord identity to Pioneer Square — "
    "that unlocks personal login keys and lets you chat directly with the Foreman."
)


def _welcome_dm_text(username: str) -> str:
    """Return the welcome DM body, filling in *username* if the template uses it.

    Reads ``DISCORD_WELCOME_DM_TEXT`` on every call (rather than caching) so an
    operator's env var change takes effect without a restart. Falls back to the
    literal template when it doesn't reference ``{username}`` — or references an
    unknown placeholder — so a misconfigured env var never blocks the DM.
    """
    template = os.environ.get("DISCORD_WELCOME_DM_TEXT") or _DEFAULT_WELCOME_DM_TEXT
    try:
        return template.format(username=username)
    except (KeyError, IndexError) as e:
        logger.warning("DISCORD_WELCOME_DM_TEXT render failed, using raw template: %s", e)
        return template


async def send_welcome_dm(discord_user_id: str, username: str | None = None) -> None:
    """DM a newly-joined guild member with onboarding instructions.

    Opens a DM channel with the bot (``POST /users/@me/channels``), then posts
    the welcome message there. Some users have DMs disabled for non-friends —
    Discord's API returns an HTTP error opening the channel or sending the
    message in that case, which is caught and logged at WARNING. Silent
    no-op when the bot token is not configured. Never raises.
    """
    if not _bot_token():
        return
    try:
        dm_channel = await _bot_request_raw(
            "post", "/users/@me/channels", {"recipient_id": discord_user_id}
        )
        channel_id = dm_channel.get("id")
        if not channel_id:
            logger.warning(
                "discord: could not open DM channel for new member user=%s", discord_user_id
            )
            return

        content = _welcome_dm_text(username or "there")
        await _bot_request_raw(
            "post", f"/channels/{channel_id}/messages", {"content": content[:_MAX_MESSAGE_LENGTH]}
        )
    except httpx.HTTPError as e:
        logger.warning("discord: welcome DM failed for new member user=%s: %s", discord_user_id, e)


# ---------------------------------------------------------------------------
# Phase 3: Foreman chat threads
# ---------------------------------------------------------------------------


async def _lookup_task_issue_coords(task_id: str) -> tuple[str, int] | None:
    """Return the ``(issue_repo, issue_number)`` linked to *task_id*, or None.

    Used to route task-scoped Foreman chat into the same thread as that
    task's PR/issue notifications (see ``_lookup_thread``), instead of the
    daily guild thread. None when the task has no linked issue/PR, doesn't
    exist, or the lookup fails.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Task  # noqa: PLC0415
        from sqlalchemy.exc import NoResultFound  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        try:
            async with AsyncSessionLocal() as db:
                result = await db.exec(
                    select(Task.issue_repo, Task.issue_number).where(col(Task.id) == task_id)
                )
                row = result.one_or_none()
                if row and row[0] and row[1] is not None:
                    return row[0], row[1]
                return None
        except NoResultFound:
            # Expected when the task has no linked issue/PR (or no thread) yet.
            logger.debug("discord: no issue coords for task=%s", task_id)
            return None
    except Exception as exc:
        logger.warning(
            "discord: task issue-coords lookup failed task=%s error_type=%s: %s",
            task_id,
            type(exc).__name__,
            exc,
        )
        return None


# Discord's hard cap on a single message's content length.
_MAX_MESSAGE_LENGTH = 2000


async def _post_foreman_chat_line(thread_id: str, content: str) -> None:
    """POST *content* to *thread_id*, applying the shared length guardrail.

    Both the per-task and daily-thread paths in ``notify_foreman_chat`` route
    through this helper so neither can skip the truncation applied to the
    other.
    """
    await _bot_request(
        "post",
        f"/channels/{thread_id}/messages",
        {"content": content[:_MAX_MESSAGE_LENGTH]},
    )


async def notify_foreman_chat(
    guild_id: str,
    content: str,
    task_id: str | None = None,
) -> None:
    """Mirror a Foreman → user chat line into Discord.

    When *task_id* is given, its issue-rooted task tree takes priority: if
    walking ``parent_task_id`` up to the root task finds a
    ``discord_thread_id``, the line posts there. Otherwise, when *task_id*
    resolves (via ``discord_threads``) to a per-task thread already created
    for that task's linked PR/issue, the line is posted there. In every other
    case — no *task_id*, or a *task_id* with no root or linked thread yet —
    the line is posted directly to the guild's main configured Discord
    channel; there is no dated/daily fallback thread.

    The message is only ever posted to one destination. Silent no-op when
    the bot token or channel are not configured, or when *content* is
    blank. Never raises.
    """
    if not content or not content.strip():
        return
    if not _bot_token():
        return

    channel = await _resolve_channel_for_guild(guild_id)
    if not channel:
        return

    if task_id:
        root_thread_id = await _resolve_root_thread_id(task_id)
        if root_thread_id:
            await _post_foreman_chat_line(root_thread_id, content)
            return

        coords = await _lookup_task_issue_coords(task_id)
        if coords:
            issue_repo, issue_number = coords
            task_thread_id = await _lookup_thread(issue_repo, issue_number)
            if task_thread_id:
                await _post_foreman_chat_line(task_thread_id, content)
                return

    await _post_foreman_chat_line(channel, content)


# ---------------------------------------------------------------------------
# Phase 3: GitHub login -> Discord user mentions
# ---------------------------------------------------------------------------


async def _lookup_discord_user(github_login: str) -> str | None:
    """Return the Discord user ID mapped to *github_login*, or None."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordUser  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordUser.discord_user_id).where(
                    col(DiscordUser.github_login) == github_login.lower()
                )
            )
            return result.one_or_none()
    except Exception:
        logger.warning("discord: user mapping lookup failed login=%s", github_login, exc_info=True)
        return None


async def mention_or_login(github_login: str | None) -> str:
    """Return a Discord @-mention for *github_login* when a mapping exists.

    Falls back to a plain ``@login`` string when no mapping is found, and to
    ``""`` when *github_login* is empty — callers never need to special-case
    a missing mapping. Never raises.
    """
    if not github_login:
        return ""
    discord_id = await _lookup_discord_user(github_login)
    return f"<@{discord_id}>" if discord_id else f"@{github_login}"


# ---------------------------------------------------------------------------
# Phase 2: thread-aware public API
# ---------------------------------------------------------------------------


async def notify_event(
    event_type: str,
    title: str,
    description: str,
    url: str | None = None,
    color: int | None = None,
    issue_repo: str | None = None,
    issue_number: int | None = None,
    thread_name: str | None = None,
    header_fields: dict[str, str] | None = None,
    close: bool = False,
    ps_guild_slug: str | None = None,
    linked_issue_repo: str | None = None,
    linked_issue_number: int | None = None,
    task_id: str | None = None,
) -> None:
    """Post a Discord notification, routing to a per-PR/issue thread when possible.

    Thread routing is attempted when ``DISCORD_BOT_TOKEN`` is set **and** both
    ``issue_repo`` and ``issue_number`` are provided.  The thread is lazily
    created on first use and its ID cached in the ``discord_threads`` table.

    When *task_id* is given, resolving its issue-rooted task tree takes
    priority over everything else: ``_resolve_root_thread_id`` walks
    ``parent_task_id`` up to the phase='issue' root task and, if that root has
    a ``discord_thread_id``, posts there directly (*close* is ignored — the
    root thread outlives any single child task). Falls back to the behaviour
    below when *task_id* has no root, or its root has no thread yet (legacy
    tasks predating the issue-rooted tree).

    When *linked_issue_repo*/*linked_issue_number* are given (a PR's linked
    GitHub issue, e.g. via ``Closes #N`` or the task's ``issue_number``) and
    that issue already has a Discord thread on record, the message is posted
    there instead — no new PR-numbered thread is created, and *close* is
    ignored (the issue's thread outlives any single PR closing/merging into
    it). Falls back to the ``issue_repo``/``issue_number`` (PR-keyed) behaviour
    below when the linked issue has no thread yet.

    When *linked_issue_repo*/*linked_issue_number* are omitted but *task_id*
    is given, the linked issue is resolved from ``Task.issue_repo``/
    ``Task.issue_number`` (same lookup ``notify_foreman_chat`` uses) — a
    convenience for callers that only have a task_id on hand.

    When *ps_guild_slug* is provided, the destination channel is resolved via
    the ``discord_channel_guilds`` binding table (populated by
    ``/join-channel``) before falling back to the flat ``DISCORD_CHANNEL_ID``
    env var.

    When ``close=True`` the thread is archived after the message is posted
    (used for PR merge/close events).

    Falls back to a flat embed posted to the resolved channel when thread
    operations fail. Silent no-op when the bot token is not configured.
    Never raises.
    """
    if _bot_token() and task_id:
        root_thread_id = await _resolve_root_thread_id(task_id)
        if root_thread_id:
            await _post_to_thread(
                root_thread_id,
                event_type,
                title,
                description,
                url=url,
                color=color,
                fields=header_fields,
            )
            return

    if _bot_token() and not (linked_issue_repo and linked_issue_number is not None) and task_id:
        coords = await _lookup_task_issue_coords(task_id)
        if coords:
            linked_issue_repo, linked_issue_number = coords

    if _bot_token() and linked_issue_repo and linked_issue_number is not None:
        linked_thread_id = await _lookup_thread(linked_issue_repo, linked_issue_number)
        if linked_thread_id:
            await _post_to_thread(
                linked_thread_id,
                event_type,
                title,
                description,
                url=url,
                color=color,
                fields=header_fields,
            )
            return

    if _bot_token() and issue_repo and issue_number is not None:
        tn = thread_name or f"#{issue_number}: {title}"
        channel = await _resolve_channel_for_guild(ps_guild_slug)
        thread_id = await _ensure_thread(issue_repo, issue_number, tn, channel=channel)
        if thread_id:
            await _post_to_thread(
                thread_id,
                event_type,
                title,
                description,
                url=url,
                color=color,
                fields=header_fields,
            )
            if close:
                await archive_thread(thread_id)
            return

    # Fallback: flat embed to the resolved channel
    await notify(event_type, title, description, url=url, color=color, ps_guild_slug=ps_guild_slug)


async def notify_existing_thread(
    event_type: str,
    title: str,
    description: str,
    issue_repo: str | None = None,
    issue_number: int | None = None,
    url: str | None = None,
    color: int | None = None,
    task_id: str | None = None,
) -> None:
    """Post to an issue/PR's Discord thread only if one already exists.

    This is deliberately **not** a thin wrapper around ``notify_event``:
    ``notify_event`` always creates a thread when none exists yet, naming it
    from the *current* call's ``title`` (or an explicit ``thread_name``). For
    mid-lifecycle notifications like follow-up/redirect, that title is a
    transient string such as ``"Follow-up sent: t-123"`` — not the issue's
    title. If the thread ``assign_task`` was supposed to create is missing
    (e.g. Discord was unconfigured at task start, or that call failed), using
    ``notify_event`` here would silently spawn a new thread mistitled after
    the follow-up itself rather than the issue, and every later notification
    would then be scattered across that wrong thread.

    ``notify_existing_thread`` avoids that by only ever looking up the thread
    persisted in ``discord_threads``; when there isn't one, it falls back to a
    flat embed on the configured channel instead of creating anything. Silent
    no-op / never raises, same guarantees as ``notify_event``.

    When *task_id* is given, its issue-rooted task tree takes priority: if
    walking ``parent_task_id`` up to the root task finds a
    ``discord_thread_id``, the message posts there and nothing else runs.
    Falls back to the ``issue_repo``/``issue_number`` lookup (and its flat-
    channel fallback) exactly as before when there's no root or the root has
    no thread yet.
    """
    if _bot_token() and task_id:
        root_thread_id = await _resolve_root_thread_id(task_id)
        if root_thread_id:
            await _post_to_thread(root_thread_id, event_type, title, description, url=url, color=color)
            return

    if _bot_token() and issue_repo and issue_number is not None:
        thread_id = await _lookup_thread(issue_repo, issue_number)
        if thread_id:
            await _post_to_thread(thread_id, event_type, title, description, url=url, color=color)
            return
        logger.debug(
            "notify_existing_thread: no thread on record for %s#%s, falling back to flat channel",
            issue_repo,
            issue_number,
        )
    elif not is_configured():
        logger.debug(
            "notify_existing_thread: Discord not configured, skipping event=%s",
            event_type,
        )

    await notify(event_type, title, description, url=url, color=color)


# ---------------------------------------------------------------------------
# Phase 4: live task-stream mirroring
# ---------------------------------------------------------------------------

# Discord message flag: SUPPRESS_NOTIFICATIONS. Marks a message "silent" so it
# posts without pinging channel members or firing a push notification — exactly
# the low-priority behaviour we want for a firehose of streaming task output.
_SILENT_FLAG = 1 << 12  # 4096

# How long to accumulate stream lines before flushing them as one Discord
# message. Batching is essential: worker terminal output is line-per-event and
# would blow past Discord's per-channel rate limit if each line were its own POST.
_STREAM_FLUSH_INTERVAL = 4.0  # seconds

# Force an early flush once the buffer reaches this many characters so a busy
# task's feed stays live instead of waiting out the full interval.
_STREAM_FLUSH_CHARS = 1500


def _stream_enabled() -> bool:
    """True when task-stream mirroring is switched on via ``DISCORD_STREAM_TASKS``.

    Off by default: streaming a task's full terminal feed into Discord is
    high-volume and opt-in. Requires a bot token as well, since the feed is
    always routed into a per-task thread (never a flat channel post).
    """
    flag = os.environ.get("DISCORD_STREAM_TASKS", "").strip().lower()
    return bool(_bot_token()) and flag in ("1", "true", "yes", "on")


@dataclass
class _StreamEntry:
    """One buffered stream line plus the structured tool detail that produced it."""

    line: str
    detail: dict | None = None


@dataclass
class _StreamBuffer:
    """In-memory accumulator for one task's pending stream lines."""

    guild_id: str
    lines: list[_StreamEntry] = field(default_factory=list)
    size: int = 0
    flush_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Running count of turns already posted for this task, so turn numbers stay
    # sequential across flush batches instead of resetting to 1 each time.
    turn_count: int = 0


# Keyed by task_id. Holds only tasks with un-flushed lines; entries are removed
# by ``flush_task_stream`` when the task reaches a terminal state.
_stream_buffers: dict[str, _StreamBuffer] = {}


async def _lookup_task_thread(task_id: str) -> str | None:
    """Return the persisted Discord thread_id for a task's stream, or None."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415 — lazy to avoid circular import
        from models import DiscordTaskThread  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordTaskThread).where(col(DiscordTaskThread.task_id) == task_id)
            )
            row = result.one_or_none()
            return row.thread_id if row else None
    except Exception:
        logger.warning("discord: task thread DB lookup failed task=%s", task_id, exc_info=True)
        return None


async def _save_task_thread(task_id: str, thread_id: str) -> None:
    """Persist a task→thread mapping using an atomic upsert (ignore duplicates)."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordTaskThread  # noqa: PLC0415
        from sqlalchemy.dialects.postgresql import insert  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            stmt = (
                insert(DiscordTaskThread)
                .values(
                    task_id=task_id,
                    thread_id=thread_id,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing()
            )
            await db.execute(stmt)
            await db.commit()
    except Exception:
        logger.warning(
            "discord: task thread DB save failed task=%s thread=%s",
            task_id,
            thread_id,
            exc_info=True,
        )


async def _lookup_task_description(task_id: str) -> str | None:
    """Return *task_id*'s description (for naming its thread), or None."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Task  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(select(Task.description).where(col(Task.id) == task_id))
            return result.one_or_none()
    except Exception:
        logger.warning("discord: task description lookup failed task=%s", task_id, exc_info=True)
        return None


async def _ensure_task_thread(task_id: str, channel: str) -> str | None:
    """Return the Discord thread_id for *task_id*'s stream, creating it if needed.

    Returns None on Discord API error. Never raises.
    """
    existing = await _lookup_task_thread(task_id)
    if existing:
        return existing

    description = await _lookup_task_description(task_id)
    thread_name = f"⚙ {task_id}: {description}" if description else f"⚙ {task_id}"

    new_thread_id = await _create_thread_in_channel(channel, thread_name)
    if not new_thread_id:
        return None

    await _save_task_thread(task_id, new_thread_id)
    # Re-fetch after save: on_conflict_do_nothing means a concurrent creator wins;
    # re-fetching ensures both callers converge on the same persisted thread_id.
    return await _lookup_task_thread(task_id) or new_thread_id


def _chunk_content(text: str, size: int) -> list[str]:
    """Split *text* into pieces no longer than *size*, preferring newline breaks."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _format_stream_entries(entries: list[_StreamEntry], start_turn: int) -> tuple[str, int]:
    """Compact consecutive tool_use/tool_result entries into per-turn summary lines.

    Mirrors the frontend's turn grouping (see LogList.vue): a run of consecutive
    tool_use/tool_result entries between non-tool lines (assistant text, thinking,
    turn-completion markers) becomes one ``▶ Turn N: edit × 2, bash × 1`` line
    instead of dumping every raw tool call and result into the thread. Non-tool
    lines pass through unchanged. Returns the formatted text and the turn count
    to carry into the next batch, so numbering stays sequential across flushes.
    """
    output: list[str] = []
    group: list[_StreamEntry] = []
    turn_number = start_turn

    def flush_group() -> None:
        nonlocal turn_number, group
        if not group:
            return
        turn_number += 1
        counts: dict[str, int] = {}
        for entry in group:
            detail = entry.detail or {}
            if detail.get("toolType") == "tool_use":
                # Lowercase intentionally, to match the frontend's formatTurnCounts
                # (LogList.vue) so tool names group consistently regardless of case.
                name = str(detail.get("name") or "tool").lower()
                counts[name] = counts.get(name, 0) + 1
        parts = ", ".join(f"{name} × {count}" for name, count in counts.items())
        output.append(f"▶ Turn {turn_number}: {parts or 'tool activity'}")
        group = []

    for entry in entries:
        detail = entry.detail or {}
        if detail.get("toolType") in ("tool_use", "tool_result"):
            group.append(entry)
        else:
            flush_group()
            output.append(entry.line)
    flush_group()
    return "\n".join(output), turn_number


async def _post_task_stream(guild_id: str, task_id: str, content: str) -> None:
    """Post *content* into *task_id*'s per-task thread as silent messages. Never raises."""
    channel = await _resolve_channel_for_guild(guild_id)
    if not channel:
        return
    thread_id = await _ensure_task_thread(task_id, channel)
    if not thread_id:
        return
    for chunk in _chunk_content(content, _MAX_MESSAGE_LENGTH):
        if not chunk:
            continue
        await _bot_request(
            "post",
            f"/channels/{thread_id}/messages",
            {
                "content": chunk,
                "flags": _SILENT_FLAG,
                # Never let streamed build output @-mention anyone.
                "allowed_mentions": {"parse": []},
            },
        )


async def _drain_and_post(buf: _StreamBuffer, task_id: str) -> None:
    """Atomically swap out *buf*'s pending lines and post them. Never raises."""
    async with buf.lock:
        if not buf.lines:
            return
        content, buf.turn_count = _format_stream_entries(buf.lines, buf.turn_count)
        buf.lines = []
        buf.size = 0
    await _post_task_stream(buf.guild_id, task_id, content)


async def _flush_task_stream(task_id: str) -> None:
    """Flush the pending buffer for *task_id* if it is still registered."""
    buf = _stream_buffers.get(task_id)
    if buf is None:
        return
    await _drain_and_post(buf, task_id)


async def _delayed_flush(task_id: str) -> None:
    """Wait out the batching interval, then flush *task_id*'s buffer."""
    try:
        await asyncio.sleep(_STREAM_FLUSH_INTERVAL)
    except asyncio.CancelledError:
        return
    buf = _stream_buffers.get(task_id)
    if buf is not None:
        buf.flush_task = None
    await _flush_task_stream(task_id)


async def notify_task_stream(
    guild_id: str, task_id: str, line: str, detail: dict | None = None
) -> None:
    """Buffer one streaming terminal line for *task_id* and schedule a flush.

    Fast and non-blocking: appends to an in-memory buffer and (re)arms a
    background flush task — the actual Discord POST happens off the caller's
    path so a worker's terminal firehose never stalls the WebSocket handler.
    *detail* is the same structured tool-call payload the frontend uses (see
    ``LogDetail`` in ``frontend/src/types.ts``) — passed through so tool_use/
    tool_result lines can be folded into per-turn summaries at flush time
    (see ``_format_stream_entries``) instead of posted raw.
    Silent no-op unless ``DISCORD_STREAM_TASKS`` is enabled (and a bot token is
    configured), or when *line*/*task_id* is blank. Never raises.
    """
    if not _stream_enabled():
        return
    if not task_id or not line or not line.strip():
        return

    buf = _stream_buffers.get(task_id)
    if buf is None:
        buf = _StreamBuffer(guild_id=guild_id)
        _stream_buffers[task_id] = buf

    buf.lines.append(_StreamEntry(line=line, detail=detail))
    buf.size += len(line) + 1

    if buf.size >= _STREAM_FLUSH_CHARS:
        if buf.flush_task is not None:
            buf.flush_task.cancel()
            buf.flush_task = None
        # Flush in the background so this call stays off the network path.
        asyncio.ensure_future(_flush_task_stream(task_id))  # noqa: RUF006
    elif buf.flush_task is None:
        buf.flush_task = asyncio.ensure_future(_delayed_flush(task_id))


async def flush_task_stream(task_id: str) -> None:
    """Flush and drop *task_id*'s buffer — call when the task reaches a terminal state.

    Posts any tail lines immediately (rather than waiting out the interval) and
    frees the in-memory buffer. Silent no-op when the task has no buffered
    stream. Never raises.
    """
    buf = _stream_buffers.pop(task_id, None)
    if buf is None:
        return
    if buf.flush_task is not None:
        buf.flush_task.cancel()
        buf.flush_task = None
    await _drain_and_post(buf, task_id)
