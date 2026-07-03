"""Discord notification helpers: flat webhook (Phase 1) and per-PR/issue threads (Phase 2).

Phase 1 — flat webhook
    Set ``DISCORD_WEBHOOK_URL`` in the environment to post embeds to a single channel.

Phase 2 — per-PR/issue threads
    Set ``DISCORD_BOT_TOKEN`` **and** ``DISCORD_CHANNEL_ID`` to create/reuse one
    Discord thread per GitHub PR or issue.  The mapping is persisted in the
    ``discord_threads`` DB table so restarts never duplicate threads.

    When ``DISCORD_BOT_TOKEN`` is absent, every thread-aware call transparently
    falls back to the flat-webhook behaviour if ``DISCORD_WEBHOOK_URL`` is set.
    Both tokens absent → silent no-op.  HTTP and DB failures are always logged
    at WARNING level and never propagate.

Required bot permissions for Phase 2: ``SEND_MESSAGES``, ``MANAGE_THREADS``.

Usage::

    # Legacy flat webhook (unchanged):
    await discord_notifier.notify("task-complete", "Task done", "t-abc finished")

    # Thread-aware (routes to a per-PR thread or falls back):
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
"""

from __future__ import annotations

import logging
import os
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
    "pr-opened": 0x3498DB,  # blue
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


def _webhook_url() -> str | None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    return url if url else None


def _bot_token() -> str | None:
    return os.environ.get("DISCORD_BOT_TOKEN") or None


def _channel_id() -> str | None:
    return os.environ.get("DISCORD_CHANNEL_ID") or None


# ---------------------------------------------------------------------------
# Phase 1: flat webhook
# ---------------------------------------------------------------------------


async def notify(
    event_type: str,
    title: str,
    description: str,
    url: str | None = None,
    color: int | None = None,
) -> None:
    """Post a Discord embed to the configured webhook.

    Silent no-op when ``DISCORD_WEBHOOK_URL`` is not set.
    Never raises — HTTP errors are logged at WARNING level.
    """
    webhook_url = _webhook_url()
    if not webhook_url:
        return

    resolved_color = color if color is not None else _COLOURS.get(event_type, _DEFAULT_COLOUR)

    embed: dict = {
        "title": title,
        "description": description,
        "color": resolved_color,
    }
    if url:
        embed["url"] = url

    payload = {"embeds": [embed]}

    try:
        client = _get_client()
        resp = await client.post(webhook_url, json=payload)
        resp.raise_for_status()
    except Exception:
        logger.warning("Discord webhook notify failed (event=%s)", event_type, exc_info=True)


# ---------------------------------------------------------------------------
# Phase 2: bot-based thread helpers
# ---------------------------------------------------------------------------


async def _bot_request(
    method: str,
    path: str,
    json_body: dict | None = None,
) -> dict | None:
    """Make an authenticated Discord bot API request.

    Returns the parsed JSON response dict, an empty dict for 204/empty bodies,
    or None on error.  Never raises.
    """
    token = _bot_token()
    if not token:
        return None
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    try:
        client = _get_client()
        resp = await getattr(client, method)(
            f"{_DISCORD_API_BASE}{path}",
            json=json_body,
            headers=headers,
        )
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return {}
    except Exception:
        logger.warning(
            "Discord bot API request failed method=%s path=%s", method, path, exc_info=True
        )
        return None


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


async def _ensure_thread(issue_repo: str, issue_number: int, thread_name: str) -> str | None:
    """Return the Discord thread_id for a PR/issue, creating it if necessary.

    For standard text channels, thread creation requires a starter message first:
    POST /channels/{channel}/messages → get message_id
    POST /channels/{channel}/messages/{message_id}/threads → get thread_id

    Returns None when bot token or channel ID are not configured, or on API error.
    """
    existing = await _lookup_thread(issue_repo, issue_number)
    if existing:
        return existing

    channel = _channel_id()
    if not channel:
        return None

    # Step 1: post a starter message to anchor the thread
    starter = await _bot_request(
        "post",
        f"/channels/{channel}/messages",
        {"content": thread_name[:100]},
    )
    if not starter:
        return None
    message_id = starter.get("id")
    if not message_id:
        logger.warning(
            "discord: starter message returned no id for repo=%s #%s", issue_repo, issue_number
        )
        return None

    # Step 2: create the thread from that starter message
    data = await _bot_request(
        "post",
        f"/channels/{channel}/messages/{message_id}/threads",
        {"name": thread_name[:100]},
    )
    if not data:
        return None

    new_thread_id = data.get("id")
    if not new_thread_id:
        logger.warning(
            "discord: thread creation returned no id for repo=%s #%s", issue_repo, issue_number
        )
        return None

    await _save_thread(issue_repo, issue_number, new_thread_id)
    return new_thread_id


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
) -> None:
    """Post a Discord notification, routing to a per-PR/issue thread when possible.

    Thread routing is attempted when ``DISCORD_BOT_TOKEN`` is set **and** both
    ``issue_repo`` and ``issue_number`` are provided.  The thread is lazily
    created on first use and its ID cached in the ``discord_threads`` table.

    When ``close=True`` the thread is archived after the message is posted
    (used for PR merge/close events).

    Falls back to the flat webhook when the bot token is absent or thread
    operations fail.  Silent no-op when neither token is configured.
    Never raises.
    """
    if _bot_token() and issue_repo and issue_number is not None:
        tn = thread_name or f"#{issue_number}: {title}"
        thread_id = await _ensure_thread(issue_repo, issue_number, tn)
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

    # Fallback: flat webhook
    await notify(event_type, title, description, url=url, color=color)
