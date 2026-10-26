"""Queries over the ``github_events`` webhook-delivery log.

Companion to ``db/github_cache.py`` (which caches issue/PR *state*): this
module reads the append-only per-delivery ``GithubEvent`` rows written by
``routes/webhooks.py``.
"""

from __future__ import annotations

from models import GithubEvent, GithubIssue, GithubPullRequest, Task
from sqlalchemy import and_, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession


def conversation_event_filter(conversation_id: int):
    """Shared PR activity is visible to every task-linked conversation, within its guild."""
    stamped = select(GithubEvent.id).where(col(GithubEvent.conversation_id) == conversation_id)
    linked = (
        select(GithubEvent.id)
        .join(
            Task,
            and_(
                col(Task.guild_id) == col(GithubEvent.guild_id),
                col(Task.pr_repo) == col(GithubEvent.repo),
                col(Task.pr_number) == col(GithubEvent.pr_number),
            ),
        )
        .where(col(Task.conversation_id) == conversation_id, col(Task.deleted_at).is_(None))
    )
    # Separate indexed branches avoid a correlated scan of the entire delivery log.
    return col(GithubEvent.id).in_(stamped.correlate(None).union(linked.correlate(None)))


async def link_task_context(db: AsyncSession, task_id: str) -> None:
    """Attach previously unlinked PR deliveries/caches when a task gains coordinates.

    Never overwrites an existing owner or replays historical automated actions.
    The caller commits this with its task update.
    """
    task = (await db.exec(select(Task).where(col(Task.id) == task_id))).first()
    if task is None or task.conversation_id is None or task.deleted_at is not None:
        return
    for model, repo, number in (
        (GithubIssue, task.issue_repo, task.issue_number),
        (GithubPullRequest, task.pr_repo, task.pr_number),
    ):
        if repo and number is not None:
            await db.exec(
                update(model)
                .where(
                    col(model.repo) == repo,
                    col(model.number) == number,
                    col(model.conversation_id).is_(None),
                )
                .values(conversation_id=task.conversation_id)
            )
    if task.pr_repo and task.pr_number is not None:
        await db.exec(
            update(GithubEvent)
            .where(
                col(GithubEvent.guild_id) == task.guild_id,
                col(GithubEvent.repo) == task.pr_repo,
                col(GithubEvent.pr_number) == task.pr_number,
                col(GithubEvent.task_id).is_(None),
                col(GithubEvent.conversation_id).is_(None),
            )
            .values(task_id=task.id, conversation_id=task.conversation_id)
        )


async def list_events_by_conversation(
    db: AsyncSession, conversation_id: int, *, limit: int = 100
) -> list[GithubEvent]:
    """Return stamped activity plus shared PR activity for this conversation's tasks."""
    result = await db.exec(
        select(GithubEvent)
        .where(conversation_event_filter(conversation_id))
        .order_by(col(GithubEvent.id).desc())
        .limit(limit)
    )
    return list(result.all())
