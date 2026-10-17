"""Periodic conversation health sweep (#1160/#1163, Conversation-first since #1299).

``sweep_conversations`` runs once per guild on every foreman periodic-check cycle
(wired in ``foreman/runner.py``, alongside ``_sweep_closed_issues`` and
``_refresh_stale_github_links``):

1. Auto-archive ``active`` conversations that have had no activity
   (``updated_at``) for ``THREAD_AUTO_ARCHIVE_AFTER_SECONDS``.
2. Auto-close ``archived`` conversations that have sat archived for
   ``THREAD_AUTO_CLOSE_AFTER_SECONDS``.

A summary is logged to Discord via ``discord_notifier.notify`` only when the
sweep actually changed something — a quiet guild produces no chatter.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from database import get_db
from foreman.conversation_service import set_conversation_status
from models import Conversation
from sqlmodel import col, select

logger = logging.getLogger(__name__)

# How long an "active" conversation can go without activity before it's
# auto-archived. Configurable for tests/tuning; defaults to 3 days.
CONVERSATION_AUTO_ARCHIVE_AFTER_SECONDS = float(
    os.environ.get("THREAD_AUTO_ARCHIVE_AFTER_SECONDS", str(3 * 24 * 3600))
)
# How long an "archived" conversation stays archived before it's auto-closed.
# Defaults to 14 days.
CONVERSATION_AUTO_CLOSE_AFTER_SECONDS = float(
    os.environ.get("THREAD_AUTO_CLOSE_AFTER_SECONDS", str(14 * 24 * 3600))
)


async def sweep_conversations(guild_id: str) -> dict[str, int]:
    """Run the conversation-health sweep for one guild. Returns a counts summary.

    Best effort: never raises. A DB error is logged and the sweep returns a
    zeroed summary rather than aborting the caller's periodic-check cycle.
    """
    summary = {"archived": 0, "closed": 0}
    try:
        summary = await _sweep_conversations_once(guild_id)
    except Exception:
        logger.exception("conversation_maintenance: sweep failed for guild=%s", guild_id)
        return summary

    if any(summary.values()):
        await _notify_summary(guild_id, summary)
    return summary


async def _sweep_conversations_once(guild_id: str) -> dict[str, int]:
    from auth_deps import get_guild_pk  # noqa: PLC0415 — avoid import cycle at module load

    now = datetime.now(UTC)
    archive_cutoff = now - timedelta(seconds=CONVERSATION_AUTO_ARCHIVE_AFTER_SECONDS)
    close_cutoff = now - timedelta(seconds=CONVERSATION_AUTO_CLOSE_AFTER_SECONDS)

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return {"archived": 0, "closed": 0}

        stale_active = (
            await db.exec(
                select(Conversation).where(
                    col(Conversation.guild_id) == guild_pk,
                    col(Conversation.status) == "active",
                    col(Conversation.updated_at) < archive_cutoff,
                )
            )
        ).all()
        stale_archived = (
            await db.exec(
                select(Conversation).where(
                    col(Conversation.guild_id) == guild_pk,
                    col(Conversation.status) == "archived",
                    col(Conversation.updated_at) < close_cutoff,
                )
            )
        ).all()

        # Each transition commits, broadcasts ``conversation-updated`` and
        # mirrors to Discord (archive) — see set_conversation_status.
        for conversation in stale_active:
            await set_conversation_status(db, conversation, "archived")
        for conversation in stale_archived:
            await set_conversation_status(db, conversation, "closed")
    finally:
        await db.close()

    return {"archived": len(stale_active), "closed": len(stale_archived)}


async def _notify_summary(guild_id: str, summary: dict[str, int]) -> None:
    """Log a significant-changes summary to Discord. Never raises."""
    import discord_notifier  # noqa: PLC0415 — avoid import cycle at module load

    if not discord_notifier.is_configured():
        return
    lines = []
    if summary["archived"]:
        lines.append(f"{summary['archived']} conversation(s) auto-archived (idle)")
    if summary["closed"]:
        lines.append(f"{summary['closed']} conversation(s) auto-closed (idle while archived)")
    await discord_notifier.notify(
        "thread-maintenance",
        title="Conversation maintenance sweep",
        description="\n".join(lines),
        ps_guild_slug=guild_id,
    )
