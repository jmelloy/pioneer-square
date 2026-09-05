"""Discord notification helpers, all driven by the bot (gateway app): flat-channel
embeds, per-PR/issue threads, Foreman chat threads + user mentions, and live
task-stream mirroring.

Everything requires the bot. Set ``DISCORD_BOT_TOKEN`` **and** ``DISCORD_CHANNEL_ID``
to post embeds to the configured channel and to create/reuse one Discord thread per
GitHub PR or issue. Every thread this module creates or reads is a row in the single
``discord_thread_bindings`` table (see ``models.DiscordThreadBinding``), keyed on
``(subject_type, subject_key)`` — ``"issue"`` for PR/issue threads, ``"task_stream"``
for per-task live-output threads (Phase 4 below) — so restarts never duplicate
threads and both kinds share one lookup/create path (``_get_or_create_thread``).

    When a thread-aware call has no ``(issue_repo, issue_number)`` — or thread
    creation fails — it falls back to a flat embed posted to the resolved channel
    via the bot. No bot token → silent no-op. HTTP and DB failures are always
    logged at WARNING level and never propagate.

Phase 3 — Foreman chat threads + user mentions
    ``notify_foreman_chat`` mirrors Foreman → user chat narration into Discord.

    When a chat line is scoped to a task (``task_id`` passed through) and that
    task's linked GitHub issue/PR already has an ``"issue"`` thread binding,
    the line is posted there. In every other case — no task context, or a
    task with no linked thread yet — the line is posted directly to the
    guild's main configured Discord channel. There is no dated/daily
    fallback thread.

    ``mention_or_login`` resolves a GitHub login to a real ``<@id>`` Discord
    mention by joining ``discord_account_links`` (populated by the
    ``/connect-account`` slash command) to ``users.github_login``. Falls back
    to a plain ``@login`` string when the user has not linked a Discord
    account — never raises, never blocks a notification.

Phase 4 — live task-stream mirroring
    ``notify_task_stream`` mirrors a worker task's streaming terminal output
    (Claude's assistant/thinking text) into a dedicated per-task Discord thread
    while the task is working. Lines are buffered and flushed in batches (see
    ``_STREAM_FLUSH_INTERVAL``) to stay under Discord's rate limit, and each
    batch is posted as a *silent* message (``SUPPRESS_NOTIFICATIONS`` flag, no
    mentions parsed) so a firehose of build output never pings anyone — a
    genuinely low-priority feed. Opt-in via ``DISCORD_STREAM_TASKS`` (requires a
    bot token, since the feed always routes into a thread — never a flat channel
    post). The per-task thread binding is a ``"task_stream"`` row in
    ``discord_thread_bindings``, deliberately separate from that task's
    ``"issue"`` thread (if any) — the stream is a verbose working feed, the
    issue/PR thread holds the tidy embed summary — but both are now routable
    inbound (``discord/gateway.py``, ``discord/router.py``) through the same
    table.

    ``notify_task_stream_event`` posts a single embed into that same
    ``"task_stream"`` thread (creating it if needed) and, unlike
    ``notify_task_stream``, always runs — it's the fallback lifecycle
    notification (task-assigned, etc.) for tasks with no linked GitHub issue,
    which ``notify_event`` can't route into an ``"issue"`` thread.

Phase 5 — new-member welcome DM
    ``send_welcome_dm`` is called by the Gateway client (``discord.gateway``)
    when a ``GUILD_MEMBER_ADD`` event arrives, and DMs the new member
    onboarding instructions pointing them at ``/connect-account``. Requires
    ``DISCORD_GATEWAY_ENABLED`` plus the privileged Server Members intent (see
    ``discord.gateway``). Message text is configurable via
    ``DISCORD_WELCOME_DM_TEXT``. DM failures (e.g. DMs disabled) are logged at
    WARNING and never raised.

Phase 6 — canonical thread keyspace (#866)
    Every thread-aware entry point (``notify_event``, ``notify_existing_thread``,
    ``notify_foreman_chat``) resolves its subject to ONE canonical
    ``(repo, number)`` key via ``_canonical_coords`` before touching
    ``discord_threads``: the linked GitHub *issue* number when one can be
    determined (task linkage columns, the branch name's ``-<N>-t-<id>``
    suffix, or a ``Closes #N`` body reference in the ``github_pull_requests``
    cache), the PR number otherwise. GitHub issues and PRs share a number
    sequence per repo, so the key never collides across kinds — canonical
    resolution exists to stop *fragmentation*, where a PR's events and its
    linked issue's events used to land in two different threads.

Phase 7 — CI check debounce/combine
    ``notify_ci_check`` buffers ``check_run``/``check_suite`` completions per
    PR and flushes a single combined summary (e.g. ``"✅ 3 checks passed:
    lint, test, build"``) after ``DISCORD_PR_DEBOUNCE_SECONDS`` (default 15)
    of silence on that PR, instead of one Discord message per check — a CI
    matrix routinely fires several of these within a few seconds of each
    other. The combined message always carries ``SUPPRESS_NOTIFICATIONS``
    (see ``_SILENT_FLAG``) so CI results don't page anyone; non-CI events
    (PR opened/merged/closed, reviews) are unaffected and keep posting
    immediately at normal priority.

Phase 8 — thread-per-conversation (#1161)
    ``notify_foreman_chat`` routes ad-hoc Foreman chat (no ``task_id`` with a
    linked thread) into a per-``(guild_id, user_id)`` ``"conversation"``
    thread instead of the flat main channel, lazily created on first use and
    named from that first reply (``_ensure_conversation_thread``). Falls back
    to the flat channel only when no *user_id* is resolvable (a
    system-triggered line with nobody to key the thread on). Threads carry an
    explicit ``auto_archive_duration`` (``_CONVERSATION_AUTO_ARCHIVE_MINUTES``)
    so Discord natively archives an idle conversation rather than relying on
    the channel's own default. ``archive_conversation_thread`` explicitly
    archives a user's conversation thread when their history is cleared (see
    ``routes/foreman.py:clear_foreman_context``) — the closest existing
    "conversation closed" signal. Inbound replies in a conversation thread
    route back through the same ``discord_thread_bindings`` table as every
    other kind (see ``discord/router.py``'s ``"conversation"`` handling).

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
import re
from collections.abc import Awaitable, Callable
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
    "task-error": 0xE74C3C,  # red
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

# Discord message flag: SUPPRESS_NOTIFICATIONS. Marks a message "silent" so it
# posts without pinging channel members or firing a push notification — used
# for the streaming task-output firehose (Phase 4) and combined CI summaries
# (Phase 7), both of which are high-volume/low-urgency by design.
_SILENT_FLAG = 1 << 12  # 4096


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10)
    return _client


def bot_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN") or None


def _channel_id() -> str | None:
    return os.environ.get("DISCORD_CHANNEL_ID") or None


def is_configured() -> bool:
    """Return True if the Discord bot (gateway app) is set up.

    Cheap, side-effect-free check callers can use to skip expensive prep work
    (e.g. a live GitHub API call to resolve a thread title) when Discord
    notifications are disabled entirely.
    """
    return bool(bot_token())


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
    flags: int | None = None,
) -> None:
    """Post a Discord embed to the bot's configured channel.

    Resolves the destination channel via the ``discord_channel_guilds``
    binding for *ps_guild_slug* (when given), falling back to the flat
    ``DISCORD_CHANNEL_ID`` env var. Silent no-op when the bot token or a
    destination channel are not configured. Never raises — errors are logged
    at WARNING level by the underlying bot request.
    """
    if not bot_token():
        return

    channel = await _resolve_channel_for_guild(ps_guild_slug)
    if not channel:
        return

    await _post_to_thread(
        channel, event_type, title, description, url=url, color=color, flags=flags
    )


# ---------------------------------------------------------------------------
# Bot-based thread helpers
# ---------------------------------------------------------------------------


async def _bot_request_raw(
    method: str,
    path: str,
    json_body: dict | None = None,
) -> dict | list:
    """Make an authenticated Discord bot API request, raising on failure.

    Returns the parsed JSON response (dict or list), or an empty dict for
    204/empty bodies. Unlike ``_bot_request``, this does not swallow errors — callers
    that need to distinguish an expected API failure (``httpx.HTTPError``)
    from a genuine bug use this directly (see ``send_welcome_dm``).
    """
    token = bot_token()
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
) -> dict | list | None:
    """Make an authenticated Discord bot API request.

    Returns the parsed JSON response (dict or list), an empty dict for
    204/empty bodies, or None on error.  Never raises.
    """
    if not bot_token():
        return None
    try:
        return await _bot_request_raw(method, path, json_body)
    except Exception:
        logger.warning(
            "Discord bot API request failed method=%s path=%s", method, path, exc_info=True
        )
        return None


async def get(path: str) -> dict | list | None:
    """GET a Discord bot API endpoint. Returns parsed JSON, or None on error/no token."""
    return await _bot_request("get", path)


async def post(path: str, payload: dict | None = None) -> dict | list | None:
    """POST to a Discord bot API endpoint. Returns parsed JSON, or None on error/no token."""
    return await _bot_request("post", path, payload)


async def patch(path: str, payload: dict) -> dict | list | None:
    """PATCH a Discord bot API endpoint. Returns parsed JSON, or None on error/no token."""
    return await _bot_request("patch", path, payload)


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


_SUBJECT_ISSUE = "issue"
_SUBJECT_TASK_STREAM = "task_stream"
_SUBJECT_CONVERSATION = "conversation"


def _issue_subject_key(issue_repo: str, issue_number: int) -> str:
    return f"{issue_repo}#{issue_number}"


def _conversation_subject_key(guild_id: str, user_id: str) -> str:
    return f"{guild_id}:{user_id}"


async def _lookup_thread(subject_type: str, subject_key: str) -> str | None:
    """Return the persisted Discord thread_id bound to *subject_type*/*subject_key*, or None."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415 — lazy to avoid circular import
        from models import DiscordThreadBinding  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordThreadBinding).where(
                    col(DiscordThreadBinding.subject_type) == subject_type,
                    col(DiscordThreadBinding.subject_key) == subject_key,
                )
            )
            row = result.one_or_none()
            return row.thread_id if row else None
    except Exception:
        logger.warning(
            "discord: thread DB lookup failed subject=%s:%s",
            subject_type,
            subject_key,
            exc_info=True,
        )
        return None


async def _save_thread(subject_type: str, subject_key: str, thread_id: str) -> None:
    """Persist a new subject -> Discord thread binding using an atomic upsert (ignore duplicates)."""
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordThreadBinding  # noqa: PLC0415
        from sqlalchemy.dialects.postgresql import insert  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            stmt = (
                insert(DiscordThreadBinding)
                .values(
                    subject_type=subject_type,
                    subject_key=subject_key,
                    thread_id=thread_id,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing()
            )
            await db.execute(stmt)
            await db.commit()
    except Exception:
        logger.warning(
            "discord: thread DB save failed subject=%s:%s thread=%s",
            subject_type,
            subject_key,
            thread_id,
            exc_info=True,
        )


async def _get_or_create_thread(
    subject_type: str,
    subject_key: str,
    channel: str | None,
    thread_name: str | Callable[[], Awaitable[str]],
    *,
    auto_archive_duration: int | None = None,
) -> str | None:
    """Return the Discord thread_id bound to *subject_type*/*subject_key*, creating it if needed.

    Shared by every thread-aware path — PR/issue narration (``_ensure_thread``),
    task live-stream mirroring (``_ensure_task_thread``), and per-conversation
    Foreman chat (``_ensure_conversation_thread``) all used to carry (or would
    otherwise carry) their own copy-pasted lookup/create/save trio against
    separate tables; this is the one lookup-or-create the whole module goes
    through.

    *thread_name* may be a plain string or a zero-arg async callable — the
    callable form lets a caller defer expensive name-building (e.g. a DB
    lookup for a task's description) until it's known the thread doesn't
    already exist.

    *auto_archive_duration* (minutes; Discord accepts 60/1440/4320/10080) is
    passed straight through to thread creation when given, so a caller can
    opt into a shorter native inactivity-archive window than the channel's
    default instead of relying on it implicitly.

    Returns None when *channel* is not resolved (and no thread exists yet), or
    on Discord API error. Never raises.
    """
    existing = await _lookup_thread(subject_type, subject_key)
    if existing:
        return existing

    if not channel:
        return None

    name = await thread_name() if callable(thread_name) else thread_name
    new_thread_id = await _create_thread_in_channel(
        channel, name, auto_archive_duration=auto_archive_duration
    )
    if not new_thread_id:
        return None

    await _save_thread(subject_type, subject_key, new_thread_id)
    # Re-fetch after save: on_conflict_do_nothing means a concurrent creator wins;
    # re-fetching ensures both callers converge on the same persisted thread_id.
    return await _lookup_thread(subject_type, subject_key) or new_thread_id


async def _create_thread_in_channel(
    channel: str, thread_name: str, *, auto_archive_duration: int | None = None
) -> str | None:
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

    thread_payload: dict = {"name": thread_name[:100]}
    if auto_archive_duration is not None:
        thread_payload["auto_archive_duration"] = auto_archive_duration
    data = await _bot_request(
        "post",
        f"/channels/{channel}/messages/{message_id}/threads",
        thread_payload,
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
    channel = channel or _channel_id()
    return await _get_or_create_thread(
        _SUBJECT_ISSUE, _issue_subject_key(issue_repo, issue_number), channel, thread_name
    )


# ---------------------------------------------------------------------------
# Canonical thread key: one work item -> one (repo, number) -> one thread
# ---------------------------------------------------------------------------

# A PR's linked issue, from the branch name's "-<N>-t-<id>" suffix (always
# set by the system) or a "Closes #N"-style reference in the PR body.
# routes/webhooks.py imports these for its own linkage bookkeeping.
CLOSES_ISSUE_RE = re.compile(r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
_BRANCH_ISSUE_RE = re.compile(r"-(\d+)-t-[a-z0-9]+$")


def linked_issue_number_from_body(body: str | None) -> int | None:
    """Return the issue number a PR body closes ("Closes #N" etc.), or None."""
    if not body:
        return None
    match = CLOSES_ISSUE_RE.search(body)
    return int(match.group(1)) if match else None


def linked_issue_number_from_branch(branch: str | None) -> int | None:
    """Return the issue number encoded in a system branch name suffix, or None."""
    if not branch:
        return None
    match = _BRANCH_ISSUE_RE.search(branch)
    return int(match.group(1)) if match else None


async def _canonical_coords(
    repo: str | None,
    number: int | None,
    *,
    kind: str = "issue",
    task_id: str | None = None,
) -> tuple[str, int] | None:
    """Resolve the canonical ``(repo, number)`` thread key for a work item.

    One work item gets one thread, keyed by the GitHub *issue* number when an
    issue is linked and by the PR number otherwise. GitHub issues and PRs
    share a single number sequence per repo, so the pair never collides
    across kinds — the failure mode this prevents is *fragmentation*: the
    same work item keyed once under its PR number and once under its issue
    number (#866).

    Resolution order:
      1. the task's own issue linkage (``Task.issue_repo``/``issue_number``)
      2. for PRs (``kind='pr'``): any task linked to the PR that knows its
         issue, then the cached PR's branch-name suffix or ``Closes #N``
         body reference (``github_pull_requests``, webhook-fed)
      3. the subject ``(repo, number)`` as given

    Never raises; returns None when no coordinates can be determined at all.
    """
    pr_repo, pr_number = (repo, number) if kind == "pr" else (None, None)
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415 — lazy to avoid circular import
        from models import GithubPullRequest, Task  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            if task_id:
                result = await db.exec(
                    select(Task.issue_repo, Task.issue_number, Task.pr_repo, Task.pr_number).where(
                        col(Task.id) == task_id
                    )
                )
                row = result.one_or_none()
                if row:
                    if row[0] and row[1] is not None:
                        return row[0], row[1]
                    if pr_repo is None and row[2] and row[3] is not None:
                        pr_repo, pr_number = row[2], row[3]

            if pr_repo and pr_number is not None:
                result = await db.exec(
                    select(Task.issue_repo, Task.issue_number)
                    .where(
                        col(Task.pr_repo) == pr_repo,
                        col(Task.pr_number) == pr_number,
                        col(Task.issue_number).is_not(None),
                    )
                    .limit(1)
                )
                linked = result.first()
                if linked and linked[0]:
                    return linked[0], linked[1]

                result = await db.exec(
                    select(GithubPullRequest.head_ref, GithubPullRequest.body).where(
                        col(GithubPullRequest.repo) == pr_repo,
                        col(GithubPullRequest.number) == pr_number,
                    )
                )
                pr_row = result.one_or_none()
                if pr_row:
                    issue_num = linked_issue_number_from_branch(
                        pr_row[0]
                    ) or linked_issue_number_from_body(pr_row[1])
                    if issue_num is not None:
                        return pr_repo, issue_num
                return pr_repo, pr_number
    except Exception:
        logger.warning(
            "discord: canonical coords resolution failed repo=%s number=%s task=%s",
            repo,
            number,
            task_id,
            exc_info=True,
        )
    return (repo, number) if repo and number is not None else None


async def _issue_thread_name(repo: str, number: int) -> str | None:
    """Return "#N: <title>" from the github_issues cache, or None on a miss.

    Used when a PR event resolves to its linked issue and has to create that
    issue's thread — naming it after the issue rather than the transient PR
    event title.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import GithubIssue  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(GithubIssue.title).where(
                    col(GithubIssue.repo) == repo, col(GithubIssue.number) == number
                )
            )
            title = result.one_or_none()
            return f"#{number}: {title}" if title else None
    except Exception:
        logger.warning(
            "discord: issue thread name lookup failed %s#%s", repo, number, exc_info=True
        )
        return None


async def _post_to_thread(
    thread_id: str,
    event_type: str,
    title: str,
    description: str,
    url: str | None = None,
    color: int | None = None,
    fields: dict[str, str] | None = None,
    flags: int | None = None,
) -> None:
    """Post an embed to an existing Discord thread. Never raises.

    *flags* is the raw Discord message-flags bitmask (see ``_SILENT_FLAG``) —
    passed through so callers like ``notify_ci_check`` can mark a message
    silent without duplicating the request-building logic here.
    """
    resolved_color = color if color is not None else _COLOURS.get(event_type, _DEFAULT_COLOUR)
    embed: dict = {"title": title, "description": description, "color": resolved_color}
    if url:
        embed["url"] = url
    if fields:
        embed["fields"] = [{"name": k, "value": v, "inline": True} for k, v in fields.items()]
    payload: dict = {"embeds": [embed]}
    if flags:
        payload["flags"] = flags
    await _bot_request("post", f"/channels/{thread_id}/messages", payload)


async def archive_thread(thread_id: str) -> None:
    """Archive a Discord thread. Never raises."""
    await _bot_request("patch", f"/channels/{thread_id}", {"archived": True})


async def rename_thread(thread_id: str, name: str) -> None:
    """Rename a Discord thread. Never raises."""
    await _bot_request("patch", f"/channels/{thread_id}", {"name": name[:100]})


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
    if not bot_token():
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


# Discord's hard cap on a single message's content length.
_MAX_MESSAGE_LENGTH = 2000

# Native Discord inactivity auto-archive window for per-conversation threads
# (minutes; Discord only accepts 60/1440/4320/10080). 24h keeps a quiet
# conversation's thread around for a same-day follow-up without leaving
# long-abandoned threads open indefinitely.
_CONVERSATION_AUTO_ARCHIVE_MINUTES = 1440

# Thread name cap, well under Discord's 100-char limit, leaving room for the
# "💬 " prefix and a trailing ellipsis.
_CONVERSATION_THREAD_NAME_SNIPPET_LEN = 80


def _conversation_thread_name(content: str) -> str:
    """Build a per-conversation thread name from the Foreman's opening reply.

    Collapses whitespace/newlines and truncates to a single line so a
    multi-paragraph first reply doesn't produce an unreadable thread title.
    """
    snippet = " ".join(content.split())
    if len(snippet) > _CONVERSATION_THREAD_NAME_SNIPPET_LEN:
        snippet = snippet[: _CONVERSATION_THREAD_NAME_SNIPPET_LEN - 1].rstrip() + "…"
    return f"💬 {snippet}" if snippet else "💬 Conversation"


async def _ensure_conversation_thread(
    guild_id: str, user_id: str, channel: str, content: str
) -> str | None:
    """Return the Discord thread_id for *user_id*'s ad-hoc Foreman conversation
    in *guild_id*, creating it (named from *content*) if needed.
    """
    return await _get_or_create_thread(
        _SUBJECT_CONVERSATION,
        _conversation_subject_key(guild_id, user_id),
        channel,
        _conversation_thread_name(content),
        auto_archive_duration=_CONVERSATION_AUTO_ARCHIVE_MINUTES,
    )


async def archive_conversation_thread(guild_id: str, user_id: str) -> None:
    """Archive *user_id*'s per-conversation Discord thread in *guild_id*, if one exists.

    Called when that conversation's history is cleared (see
    ``routes/foreman.py:clear_foreman_context``) — the closest existing signal
    for "this conversation is over" in the conversation state machine, so the
    thread doesn't sit around implying the conversation is still live. Silent
    no-op if no thread was ever created, or the bot isn't configured. Never
    raises (``archive_thread`` itself never raises).
    """
    if not bot_token():
        return
    thread_id = await _lookup_thread(
        _SUBJECT_CONVERSATION, _conversation_subject_key(guild_id, user_id)
    )
    if thread_id:
        await archive_thread(thread_id)


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
    channel_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Mirror a Foreman → user chat line into Discord.

    When *channel_id* is given it wins outright: the line goes there and
    nothing else is consulted. That is the @-mention reply path (see
    ``discord/router.py``), where the answer belongs in the channel or DM the
    mention arrived in — which may not be wired to *guild_id* at all, so the
    guild's configured channel is not a meaningful fallback for it.

    Otherwise, when *task_id* is given and its canonical issue/PR (see
    ``_canonical_coords``) already has a thread in ``discord_threads``, the
    line is posted there — this lookup never *creates* a thread (see
    ``notify_existing_thread``'s docstring for why).

    Otherwise, when *user_id* is given, the line is posted to that user's
    per-conversation thread in *guild_id* (``"conversation"`` subject,
    ``_ensure_conversation_thread``), created on first use and named from
    this call's *content* — the thread-per-conversation model (#1161): ad-hoc
    Foreman chat no longer clutters the main channel.

    In every other case — no *task_id* with a linked thread, and no
    *user_id* (e.g. a system-triggered line with no resolvable user) — the
    line is posted directly to the guild's main configured Discord channel.

    The message is only ever posted to one destination. Silent no-op when
    the bot token or channel are not configured, or when *content* is
    blank. Never raises.
    """
    if not content or not content.strip():
        return
    if not bot_token():
        return

    if channel_id:
        await _post_foreman_chat_line(channel_id, content)
        return

    channel = await _resolve_channel_for_guild(guild_id)
    if not channel:
        return

    if task_id:
        coords = await _canonical_coords(None, None, task_id=task_id)
        if coords:
            task_thread_id = await _lookup_thread(_SUBJECT_ISSUE, _issue_subject_key(*coords))
            if task_thread_id:
                await _post_foreman_chat_line(task_thread_id, content)
                return
    elif user_id:
        # Issue #1168: prefer the Foreman Thread model's discord_thread_id
        # (set by discord/thread_mirror.py when the thread was created).
        # Falls back to the legacy _ensure_conversation_thread only when no
        # Foreman-managed Discord thread exists yet.
        foreman_discord_thread = await _lookup_foreman_thread_for_user(guild_id, user_id)
        if foreman_discord_thread:
            await _post_foreman_chat_line(foreman_discord_thread, content)
            return
        conversation_thread_id = await _ensure_conversation_thread(
            guild_id, user_id, channel, content
        )
        if conversation_thread_id:
            await _post_foreman_chat_line(conversation_thread_id, content)
            return

    await _post_foreman_chat_line(channel, content)


async def _lookup_foreman_thread_for_user(guild_slug: str, user_id: str) -> str | None:
    """Return the Discord thread ID from the Foreman's active Thread for this user.

    Queries the Foreman's Conversation model (#1167/#1168/#1274) to find
    the current active thread's ``discord_thread_id`` — set by
    ``discord/thread_mirror.on_thread_created`` when the Foreman first
    created the thread, and mirrored onto ``Conversation`` by
    ``foreman.thread_service``. Returns None if no active thread or no
    Discord mirror exists. Never raises.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import Conversation, Guild  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(Conversation.discord_thread_id)
                .join(Guild, col(Guild.id) == col(Conversation.guild_id))
                .where(
                    col(Guild.slug) == guild_slug,
                    col(Conversation.user_id) == user_id,
                    col(Conversation.status) == "active",
                    col(Conversation.discord_thread_id).is_not(None),
                )
            )
            return result.first()
    except Exception:
        logger.warning(
            "discord: foreman thread lookup failed guild=%s user=%s",
            guild_slug,
            user_id,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Phase 3: GitHub login -> Discord user mentions
# ---------------------------------------------------------------------------


async def _lookup_discord_user(github_login: str) -> str | None:
    """Return the Discord user ID linked to *github_login*, or None.

    Resolves through ``users`` rather than a dedicated login -> Discord table:
    ``/connect-account`` links a Discord account to a Pioneer Square user id,
    and ``users.github_login`` is the only bridge from there back to a GitHub
    login. Compared case-insensitively — ``users.github_login`` is stored as
    GitHub returns it, so casing is not guaranteed to match the caller's.
    """
    try:
        from database import AsyncSessionLocal  # noqa: PLC0415
        from models import DiscordAccountLink, User  # noqa: PLC0415
        from sqlalchemy import func  # noqa: PLC0415
        from sqlmodel import col, select  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            result = await db.exec(
                select(DiscordAccountLink.discord_user_id)
                .join(User, col(User.id) == col(DiscordAccountLink.ps_user_id))
                .where(func.lower(col(User.github_login)) == github_login.lower())
            )
            return result.first()
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
    kind: str = "issue",
    thread_name: str | None = None,
    header_fields: dict[str, str] | None = None,
    close: bool = False,
    ps_guild_slug: str | None = None,
    task_id: str | None = None,
    flags: int | None = None,
) -> None:
    """Post a Discord notification, routing to the work item's one thread.

    ``issue_repo``/``issue_number`` are the event subject's coordinates —
    pass ``kind="pr"`` when that number is a PR number. The destination is
    the *canonical* thread key resolved by ``_canonical_coords``: the linked
    GitHub issue when one can be determined (task linkage, branch suffix, or
    ``Closes #N``), the PR itself otherwise. The thread is lazily created on
    first use and its ID cached in the ``discord_threads`` table; when a PR
    event has to create its linked issue's thread, the thread is named after
    the issue (``github_issues`` cache) rather than this event's title.

    When *ps_guild_slug* is provided, the destination channel is resolved via
    the ``discord_channel_guilds`` binding table (populated by
    ``/join-channel``) before falling back to the flat ``DISCORD_CHANNEL_ID``
    env var.

    When ``close=True`` the thread is archived after the message is posted
    (used for PR merge/close events) — unless the message landed in a linked
    issue's thread, which outlives any single PR closing/merging into it.

    *flags* is the raw Discord message-flags bitmask (see ``_SILENT_FLAG``),
    passed straight through to whichever destination the message ends up at
    — used by ``notify_ci_check`` to mark combined CI summaries silent
    without affecting any other event type's priority.

    Falls back to a flat embed posted to the resolved channel when thread
    operations fail. Silent no-op when the bot token is not configured.
    Never raises.
    """
    if bot_token():
        coords = await _canonical_coords(issue_repo, issue_number, kind=kind, task_id=task_id)
        if coords:
            repo, number = coords
            resolved_to_issue = kind == "pr" and (repo, number) != (issue_repo, issue_number)
            if resolved_to_issue:
                tn = await _issue_thread_name(repo, number) or f"#{number}"
            else:
                tn = thread_name or f"#{number}: {title}"
            channel = await _resolve_channel_for_guild(ps_guild_slug)
            thread_id = await _ensure_thread(repo, number, tn, channel=channel)
            if thread_id:
                await _post_to_thread(
                    thread_id,
                    event_type,
                    title,
                    description,
                    url=url,
                    color=color,
                    fields=header_fields,
                    flags=flags,
                )
                if close and not resolved_to_issue:
                    await archive_thread(thread_id)
                return

    # Fallback: flat embed to the resolved channel
    await notify(
        event_type,
        title,
        description,
        url=url,
        color=color,
        ps_guild_slug=ps_guild_slug,
        flags=flags,
    )


async def notify_existing_thread(
    event_type: str,
    title: str,
    description: str,
    issue_repo: str | None = None,
    issue_number: int | None = None,
    kind: str = "issue",
    url: str | None = None,
    color: int | None = None,
    task_id: str | None = None,
) -> None:
    """Post to a work item's Discord thread only if one already exists.

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

    ``notify_existing_thread`` avoids that by only ever looking up the
    ``"issue"`` thread binding — under the same canonical key ``notify_event``
    posts to (see ``_canonical_coords``) — and falling back to a flat embed on
    the configured channel when there isn't one. Silent no-op / never raises,
    same guarantees as ``notify_event``.
    """
    if bot_token():
        coords = await _canonical_coords(issue_repo, issue_number, kind=kind, task_id=task_id)
        if coords:
            thread_id = await _lookup_thread(_SUBJECT_ISSUE, _issue_subject_key(*coords))
            if thread_id:
                await _post_to_thread(
                    thread_id, event_type, title, description, url=url, color=color
                )
                return
            logger.debug(
                "notify_existing_thread: no thread on record for %s#%s, "
                "falling back to flat channel",
                coords[0],
                coords[1],
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
    return bool(bot_token()) and flag in ("1", "true", "yes", "on")


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

    async def _thread_name() -> str:
        description = await _lookup_task_description(task_id)
        return f"⚙ {task_id}: {description}" if description else f"⚙ {task_id}"

    return await _get_or_create_thread(_SUBJECT_TASK_STREAM, task_id, channel, _thread_name)


async def notify_task_stream_event(
    guild_id: str,
    task_id: str,
    event_type: str,
    title: str,
    description: str,
    color: int | None = None,
) -> None:
    """Post a Discord embed into *task_id*'s task-stream thread, creating it first.

    Fallback destination for tasks with no linked GitHub issue (e.g. created
    directly from Discord chat) — ``notify_event``'s ``"issue"`` thread has
    nothing to key off in that case and would otherwise degrade to a flat
    channel post (see its docstring). This reuses the same ``"task_stream"``
    binding/thread as ``notify_task_stream``'s live terminal-output mirroring
    (``_ensure_task_thread``), so a task never ends up with two separate
    threads — but unlike that mirroring, this always runs; it is not gated
    behind ``DISCORD_STREAM_TASKS``.

    Silent no-op when the bot token isn't configured or no channel can be
    resolved for *guild_id*. Never raises.
    """
    if not bot_token():
        return
    channel = await _resolve_channel_for_guild(guild_id)
    if not channel:
        return
    thread_id = await _ensure_task_thread(task_id, channel)
    if not thread_id:
        return
    await _post_to_thread(thread_id, event_type, title, description, color=color)


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
    instead of dumping every raw tool call and result into the thread. A run with
    at most one tool call carries no useful grouping information, so — like the
    frontend — it passes through as its raw lines (e.g. ``▶ bash: pytest``)
    instead of collapsing into a generic ``bash × 1`` turn summary. Its
    tool_result line (the actual command/tool output) is dropped entirely
    (#1107) — previously it was spoiler-wrapped (``||...||``), but a
    click-to-reveal preview was still judged too noisy since users have to
    open the thread for full context anyway. Only the tool_use line naming
    what ran is kept. Non-tool lines pass through unchanged. Returns the
    formatted text and the turn count to carry into the next batch, so
    numbering stays sequential across flushes.
    """
    output: list[str] = []
    group: list[_StreamEntry] = []
    turn_number = start_turn

    def flush_group() -> None:
        nonlocal turn_number, group
        if not group:
            return
        tool_use_count = 0
        counts: dict[str, int] = {}
        for entry in group:
            detail = entry.detail or {}
            if detail.get("toolType") == "tool_use":
                tool_use_count += 1
                # Lowercase intentionally, to match the frontend's formatTurnCounts
                # (LogList.vue) so tool names group consistently regardless of case.
                name = str(detail.get("name") or "tool").lower()
                counts[name] = counts.get(name, 0) + 1
        if tool_use_count <= 1:
            # Turn counter is intentionally NOT incremented for single-tool-call
            # groups (matching the frontend, which also skips grouping these), so
            # turn numbering may skip a number when a single-tool group sits
            # between multi-tool groups.
            for entry in group:
                detail = entry.detail or {}
                if detail.get("toolType") == "tool_result":
                    # Output is omitted entirely, not spoiler-wrapped (#1107) —
                    # the tool_use line above it already shows what ran, and
                    # the output itself doesn't add value in the preview.
                    continue
                output.append(entry.line)
            group = []
            return
        turn_number += 1
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


# ---------------------------------------------------------------------------
# Phase 7: CI check debounce/combine
# ---------------------------------------------------------------------------

# Icon + verb used when rendering the combined CI summary line, keyed by
# GitHub's check-run "conclusion" field.
_CI_CONCLUSION_ICONS: dict[str, str] = {
    "success": "✅",
    "failure": "❌",
    "cancelled": "⚠️",
    "timed_out": "⚠️",
    "action_required": "⚠️",
}

_CI_CONCLUSION_VERBS: dict[str, str] = {
    "success": "passed",
    "failure": "failed",
    "cancelled": "cancelled",
    "timed_out": "timed out",
    "action_required": "need attention",
}

# Rendering order for the combined summary: failures first (most actionable),
# then warning-ish/neutral conclusions, then successes last. Unknown
# conclusions are treated as neutral so they don't get buried below successes.
_CI_CONCLUSION_PRIORITY: dict[str, int] = {
    "failure": 0,
    "timed_out": 1,
    "action_required": 1,
    "cancelled": 1,
    "success": 2,
}


def _pr_debounce_seconds() -> float:
    """Debounce window (seconds) before flushing a PR's buffered CI checks.

    Read at call time (not cached) so ``DISCORD_PR_DEBOUNCE_SECONDS`` can be
    tuned — or overridden in tests — without a restart. Defaults to 15s.
    """
    return float(os.environ.get("DISCORD_PR_DEBOUNCE_SECONDS", "15"))


@dataclass
class _CICheckEntry:
    """One buffered check_run/check_suite completion pending combination."""

    check_name: str
    conclusion: str


@dataclass
class _CIBatch:
    """Pending CI check completions for one PR, plus the shared notify_event routing kwargs."""

    kwargs: dict
    entries: list[_CICheckEntry] = field(default_factory=list)
    flush_task: asyncio.Task | None = None


# Keyed by "{issue_repo}#{issue_number}" (or the PR label when repo/number
# aren't available). Holds only PRs with un-flushed CI entries.
_ci_batches: dict[str, _CIBatch] = {}


def _format_ci_summary(entries: list[_CICheckEntry]) -> str:
    """Combine buffered check completions into a compact, multi-line summary.

    Groups by conclusion so e.g. two passing checks plus one failing check
    renders as two lines: ``"❌ 1 check failed: build"`` followed by
    ``"✅ 2 checks passed: lint, test"`` — rather than one Discord message per
    check. Lines are ordered by ``_CI_CONCLUSION_PRIORITY`` (failures, then
    warning-ish/neutral conclusions, then successes) rather than arrival
    order, so the most actionable line is always first regardless of which
    check happened to complete first.
    """
    by_conclusion: dict[str, list[str]] = {}
    for entry in entries:
        by_conclusion.setdefault(entry.conclusion, []).append(entry.check_name)

    lines = []
    for conclusion in sorted(by_conclusion, key=lambda c: _CI_CONCLUSION_PRIORITY.get(c, 1)):
        names = by_conclusion[conclusion]
        icon = _CI_CONCLUSION_ICONS.get(conclusion, "ℹ️")
        noun = "check" if len(names) == 1 else "checks"
        verb = _CI_CONCLUSION_VERBS.get(conclusion, conclusion.replace("_", " "))
        lines.append(f"{icon} {len(names)} {noun} {verb}: {', '.join(names)}")
    return "\n".join(lines)


async def _flush_ci_batch(key: str) -> None:
    """Post the combined summary for *key*'s buffered CI checks, then drop the batch.

    Cancels the batch's own pending debounce task before popping it (guarding
    against self-cancellation when this runs from inside that very task via
    ``_delayed_ci_flush``). Without this, an early/manual flush — as tests do
    to avoid waiting out the real debounce window — leaves the original timer
    running; if a new batch is later created for the same *key* before that
    stale timer fires, it would wake up and flush the *new* batch prematurely.
    """
    batch = _ci_batches.get(key)
    if batch is None:
        return
    if (
        batch.flush_task is not None
        and batch.flush_task is not asyncio.current_task()
        and not batch.flush_task.done()
    ):
        batch.flush_task.cancel()
    _ci_batches.pop(key, None)
    if not batch.entries:
        return
    conclusions = {entry.conclusion for entry in batch.entries}
    event_type = "ci-pass" if conclusions == {"success"} else "ci-fail"
    description = _format_ci_summary(batch.entries)
    await notify_event(event_type, description=description, flags=_SILENT_FLAG, **batch.kwargs)


async def _delayed_ci_flush(key: str) -> None:
    """Wait out the debounce window, then flush *key*'s buffered CI checks."""
    try:
        await asyncio.sleep(_pr_debounce_seconds())
    except asyncio.CancelledError:
        return
    await _flush_ci_batch(key)


async def notify_ci_check(
    check_name: str,
    conclusion: str,
    *,
    pr_label: str,
    url: str | None = None,
    issue_repo: str | None = None,
    issue_number: int | None = None,
    ps_guild_slug: str | None = None,
    task_id: str | None = None,
) -> None:
    """Buffer one CI check completion for *pr_label* and (re)arm a debounced flush.

    Multiple ``check_run``/``check_suite`` completions for the same PR
    arriving within ``DISCORD_PR_DEBOUNCE_SECONDS`` of each other (a CI
    matrix routinely fires several within a few seconds) are combined into a
    single Discord message — see ``_format_ci_summary`` — instead of one
    message per check. The combined message always carries
    ``SUPPRESS_NOTIFICATIONS`` (``_SILENT_FLAG``): a CI result is useful to
    see in the thread but shouldn't page anyone. The timer starts on the
    first buffered check for a PR and is not reset by later ones in the same
    window — matching the existing task-stream buffer's batching behaviour
    (see ``notify_task_stream``) rather than a sliding debounce, so a steady
    trickle of checks can't indefinitely postpone the summary. Silent no-op
    when the bot token is not configured. Never raises.

    Guards against the batch being popped out from under it by a concurrent
    ``_flush_ci_batch(key)``: since asyncio only switches tasks at ``await``
    points and this function has none between reading and writing
    ``_ci_batches[key]``, that can only happen across two separate calls —
    e.g. a debounce timer flushes the batch this call just fetched a
    reference to, in between our ``.get()`` and this point. If so, our entry
    just landed on an object nothing will ever flush; re-registering it under
    *key* and treating it as freshly created (forcing a new flush task) keeps
    it from being silently dropped.
    """
    if not bot_token():
        return

    key = f"{issue_repo}#{issue_number}" if issue_repo and issue_number is not None else pr_label
    batch = _ci_batches.get(key)
    if batch is None:
        batch = _CIBatch(
            kwargs={
                "title": f"CI results: {pr_label}",
                "url": url,
                "issue_repo": issue_repo,
                "issue_number": issue_number,
                "kind": "pr",
                "ps_guild_slug": ps_guild_slug,
                "task_id": task_id,
            }
        )
        _ci_batches[key] = batch
    batch.entries.append(_CICheckEntry(check_name=check_name, conclusion=conclusion))

    if _ci_batches.get(key) is not batch:
        _ci_batches[key] = batch
        batch.flush_task = None

    if batch.flush_task is None or batch.flush_task.done():
        batch.flush_task = asyncio.ensure_future(_delayed_ci_flush(key))
