"""Queries over the ``github_events`` webhook-delivery log.

Companion to ``db/github_cache.py`` (which caches issue/PR *state*): this
module reads the append-only per-delivery ``GithubEvent`` rows written by
``routes/webhooks.py``.
"""

from __future__ import annotations

from models import GithubEvent
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


async def list_events_by_conversation(
    db: AsyncSession, conversation_id: int, *, limit: int = 100
) -> list[GithubEvent]:
    """Return the most recent GitHub webhook events stamped with *conversation_id*.

    Mirrors the task-scoped lookup in ``routes/debug.py`` (``GithubEvent.task_id
    == task_id``) but at the conversation level (#1277), so activity from
    every task under one conversation shows up in a single query.
    """
    result = await db.exec(
        select(GithubEvent)
        .where(col(GithubEvent.conversation_id) == conversation_id)
        .order_by(col(GithubEvent.id).desc())
        .limit(limit)
    )
    return list(result.all())
