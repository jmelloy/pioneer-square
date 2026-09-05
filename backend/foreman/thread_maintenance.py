"""Periodic thread health sweep (thread-per-conversation architecture, #1160/#1163).

``sweep_threads`` runs once per guild on every foreman periodic-check cycle
(wired in ``foreman/runner.py``, alongside ``_sweep_closed_issues`` and
``_refresh_stale_github_links``):

1. Auto-archive ``active`` threads that have had no activity
   (``updated_at``) for ``THREAD_AUTO_ARCHIVE_AFTER_SECONDS``.
2. Auto-close ``archived`` threads that have sat archived for
   ``THREAD_AUTO_CLOSE_AFTER_SECONDS``.
3. Clean up orphaned mappings: non-terminal tasks whose ``thread_id`` points
   at a thread that is now closed or soft-deleted. Left alone, a task in that
   state would keep routing Discord notifications at a thread nobody is
   watching anymore (or that no longer exists); this sweep nulls the
   reference out so the task falls back to its normal issue/task-stream
   notification routing (see ``foreman/tools.py::notify_discord_task_assigned``).

A summary is logged to Discord via ``discord_notifier.notify`` only when the
sweep actually changed something — a quiet guild produces no chatter.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from database import get_db
from foreman.thread_service import sync_conversation_after_thread_update
from models import Conversation, Task, Thread
from sqlmodel import col, select

logger = logging.getLogger(__name__)

# How long an "active" thread can go without activity before it's
# auto-archived. Configurable for tests/tuning; defaults to 3 days.
THREAD_AUTO_ARCHIVE_AFTER_SECONDS = float(
    os.environ.get("THREAD_AUTO_ARCHIVE_AFTER_SECONDS", str(3 * 24 * 3600))
)
# How long an "archived" thread stays archived before it's auto-closed.
# Defaults to 14 days.
THREAD_AUTO_CLOSE_AFTER_SECONDS = float(
    os.environ.get("THREAD_AUTO_CLOSE_AFTER_SECONDS", str(14 * 24 * 3600))
)


async def sweep_threads(guild_id: str) -> dict[str, int]:
    """Run the thread-health sweep for one guild. Returns a counts summary.

    Best effort: never raises. A DB error is logged and the sweep returns a
    zeroed summary rather than aborting the caller's periodic-check cycle.
    """
    summary = {"archived": 0, "closed": 0, "orphaned_cleaned": 0}
    try:
        summary = await _sweep_threads_once(guild_id)
    except Exception:
        logger.exception("thread_maintenance: sweep failed for guild=%s", guild_id)
        return summary

    if any(summary.values()):
        await _notify_summary(guild_id, summary)
    return summary


async def _sweep_threads_once(guild_id: str) -> dict[str, int]:
    from auth_deps import get_guild_pk  # noqa: PLC0415 — avoid import cycle at module load

    now = datetime.now(UTC)
    archive_cutoff = now - timedelta(seconds=THREAD_AUTO_ARCHIVE_AFTER_SECONDS)
    close_cutoff = now - timedelta(seconds=THREAD_AUTO_CLOSE_AFTER_SECONDS)

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return {"archived": 0, "closed": 0, "orphaned_cleaned": 0}

        stale_active = (
            await db.exec(
                select(Thread)
                .join(Conversation, col(Thread.conversation_id) == col(Conversation.id))
                .where(
                    col(Conversation.guild_id) == guild_pk,
                    col(Thread.status) == "active",
                    col(Thread.deleted_at).is_(None),
                    col(Thread.updated_at) < archive_cutoff,
                )
            )
        ).all()
        for thread in stale_active:
            thread.status = "archived"
            thread.updated_at = now
            db.add(thread)
            await sync_conversation_after_thread_update(db, thread, previous_status="active")

        stale_archived = (
            await db.exec(
                select(Thread)
                .join(Conversation, col(Thread.conversation_id) == col(Conversation.id))
                .where(
                    col(Conversation.guild_id) == guild_pk,
                    col(Thread.status) == "archived",
                    col(Thread.deleted_at).is_(None),
                    col(Thread.updated_at) < close_cutoff,
                )
            )
        ).all()
        for thread in stale_archived:
            thread.status = "closed"
            thread.updated_at = now
            db.add(thread)
            await sync_conversation_after_thread_update(db, thread, previous_status="archived")

        # Orphaned mappings: non-terminal tasks pointing at a thread that is
        # now closed or soft-deleted (includes threads just closed above,
        # since they're still tracked in the current session).
        dead_thread_ids = (
            await db.exec(
                select(col(Thread.id))
                .join(Conversation, col(Thread.conversation_id) == col(Conversation.id))
                .where(
                    col(Conversation.guild_id) == guild_pk,
                    (col(Thread.status) == "closed") | col(Thread.deleted_at).is_not(None),
                )
            )
        ).all()
        orphaned_cleaned = 0
        if dead_thread_ids:
            from task_lifecycle import TERMINAL_STATES  # noqa: PLC0415

            orphaned_tasks = (
                await db.exec(
                    select(Task).where(
                        col(Task.guild_id) == guild_pk,
                        col(Task.thread_id).in_(dead_thread_ids),
                        ~col(Task.state).in_(list(TERMINAL_STATES)),
                    )
                )
            ).all()
            for task in orphaned_tasks:
                task.thread_id = None
                db.add(task)
                orphaned_cleaned += 1

        await db.commit()
    finally:
        await db.close()

    return {
        "archived": len(stale_active),
        "closed": len(stale_archived),
        "orphaned_cleaned": orphaned_cleaned,
    }


async def _notify_summary(guild_id: str, summary: dict[str, int]) -> None:
    """Log a significant-changes summary to Discord. Never raises."""
    import discord_notifier  # noqa: PLC0415 — avoid import cycle at module load

    if not discord_notifier.is_configured():
        return
    lines = []
    if summary["archived"]:
        lines.append(f"{summary['archived']} thread(s) auto-archived (idle)")
    if summary["closed"]:
        lines.append(f"{summary['closed']} thread(s) auto-closed (idle while archived)")
    if summary["orphaned_cleaned"]:
        lines.append(f"{summary['orphaned_cleaned']} task(s) unlinked from a dead thread")
    await discord_notifier.notify(
        "thread-maintenance",
        title="Thread maintenance sweep",
        description="\n".join(lines),
        ps_guild_slug=guild_id,
    )
