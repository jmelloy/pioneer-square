"""Centralized task lifecycle transitions.

One implementation of "finalize a task" shared by every caller that ends a task:
the foreman's ``finalize_task`` tool, the closed-issue sweep, the PR
merged/closed webhooks, and the UI's finalize button. Before this module each of
those had its own copy and they had drifted — some released the task lock, some
purged queued follow-up events, some cascaded to descendants, and the UI copy did
none of it and was not TOCTOU-safe.

The terminal-state vocabulary lives here too (``TERMINAL_STATES``); it used to be
re-spelled at every call site and three of the copies were missing ``error``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from events import broadcast_msg
from lock_service import LockService
from models import Task, TaskEvent, finalize_soft_delete_at
from sqlalchemy import delete, literal, update
from sqlmodel import col, select
from ws_types import TaskFinalizeMsg, TaskUpdateMsg

logger = logging.getLogger(__name__)

# The single definition of "this task is over". ``error`` is a terminal state a
# worker reports itself — leaving it out (as three call-site copies did) makes
# guards think an errored task is still running.
TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "error"})

# Guards find_descendant_tasks against an (expected-impossible) cyclic
# parent_task_id chain so a bad row can never spin the lookup forever. Mirrors
# discord_notifier._MAX_PARENT_CHAIN_DEPTH, which walks the same chain upward.
_MAX_DESCENDANT_CHAIN_DEPTH = 20


@dataclass(slots=True)
class FinalizeResult:
    """Outcome of a :func:`finalize_task` call.

    ``status`` is ``finalized`` only when this call is the one that made the
    transition — a concurrent finalize that lost the race gets
    ``already_terminal`` and no broadcast, so the UI never sees a task flip
    states twice.
    """

    status: Literal["finalized", "already_terminal", "not_found"]
    task: Task | None = None
    deleted_at: datetime | None = None
    descendants: list[Task] = field(default_factory=list)

    @property
    def finalized(self) -> bool:
        return self.status == "finalized"


async def find_descendant_tasks(db, root_task_id: str) -> list[Task]:
    """Return every task reachable from *root_task_id* via the ``parent_task_id`` chain.

    Walks the tree downward (plan/execute/review/followup, and anything chained
    off of those) in a single recursive CTE rather than one round-trip per level.
    """
    anchor = select(col(Task.id).label("id"), literal(0).label("depth")).where(
        col(Task.parent_task_id) == root_task_id
    )
    chain = anchor.cte(name="descendant_tasks", recursive=True)
    recursive_step = (
        select(col(Task.id).label("id"), (chain.c.depth + 1).label("depth"))
        .join(chain, col(Task.parent_task_id) == chain.c.id)
        .where(chain.c.depth < _MAX_DESCENDANT_CHAIN_DEPTH - 1)
    )
    chain = chain.union_all(recursive_step)

    id_result = await db.exec(select(chain.c.id))
    ids = list(id_result.all())
    if not ids:
        return []
    task_result = await db.exec(select(Task).where(col(Task.id).in_(ids)))
    return list(task_result.all())


async def cascade_soft_delete_terminal_descendants(
    db, descendants: list[Task], deleted_at: datetime
) -> list[str]:
    """Soft-delete every already-terminal descendant task, leaving open ones alone.

    Only tasks already in a terminal state (done/failed/cancelled/error — see
    ``TERMINAL_STATES``) and not yet soft-deleted are touched. In-progress or
    pending descendants are never force-closed here; a human decides whether to
    cancel in-flight work (``finalize_closed_issue`` logs them instead). Returns
    the ids that were updated.
    """
    terminal_ids = [
        t.id for t in descendants if t.state in TERMINAL_STATES and t.deleted_at is None
    ]
    if terminal_ids:
        await db.exec(
            update(Task).where(col(Task.id).in_(terminal_ids)).values(deleted_at=deleted_at)
        )
    return terminal_ids


async def finalize_task(
    db,
    *,
    guild_pk: int,
    guild_id: str,
    task_id: str,
    outcome: str = "done",
) -> FinalizeResult:
    """Move a task to a terminal state and clean up everything that trails it.

    In order: lock the row, apply the transition, release the task lock, discard
    queued follow-up events, cascade the soft-delete to a ``phase='issue'`` root's
    already-finished descendants, commit, then broadcast.

    TOCTOU safety comes from the ``state NOT IN TERMINAL_STATES`` predicate on the
    UPDATE itself, not from the preceding SELECT — ``SELECT ... FOR UPDATE`` is a
    silent no-op on SQLite, so a read-then-write guard lets two concurrent
    finalizes both pass the check and the loser overwrites the winner's outcome.
    The SELECT is only there to read worker/issue/phase for the decisions below.

    Callers own their own side effects (Discord pings, GitHub comments) — this
    function only touches the database and the WebSocket broadcast, so no caller
    inherits another's notifications.
    """
    result = await db.exec(
        select(Task)
        .where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
        .with_for_update()
    )
    task = result.one_or_none()
    if task is None:
        return FinalizeResult("not_found")

    # done + still-open issue ⇒ stay live (deleted_at NULL) until the issue closes.
    deleted_at = finalize_soft_delete_at(outcome, task.issue_number, task.issue_state, task.phase)
    phase, worker_id = task.phase, task.worker_id
    prior_deleted_at = task.deleted_at

    upd = await db.exec(
        update(Task)
        .where(
            col(Task.id) == task_id,
            col(Task.guild_id) == guild_pk,
            col(Task.state).notin_(list(TERMINAL_STATES)),
        )
        .values(state=outcome, deleted_at=deleted_at)
    )
    if (getattr(upd, "rowcount", 0) or 0) == 0:
        return FinalizeResult("already_terminal", task=task, deleted_at=prior_deleted_at)

    await LockService(db).release(f"task:{task_id}")
    # Discard any queued follow-up events — the task is closed.
    await db.exec(delete(TaskEvent).where(col(TaskEvent.task_id) == task_id))

    # phase='issue' root tasks own an entire GitHub issue's worth of work —
    # cascade the soft-delete to descendants that already finished, but never
    # force-close in-progress/pending ones (a human decides whether to cancel
    # in-flight work). Non-issue tasks have no such ownership, so no cascade.
    descendants: list[Task] = []
    if phase == "issue":
        descendants = await find_descendant_tasks(db, task_id)
        await cascade_soft_delete_terminal_descendants(
            db, descendants, deleted_at or datetime.now(UTC)
        )

    await db.commit()

    await broadcast_msg(guild_id, TaskFinalizeMsg(workerId=worker_id, taskId=task_id))
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(
            taskId=task_id,
            state=outcome,
            deletedAt=deleted_at.isoformat() if deleted_at is not None else None,
        ),
    )
    return FinalizeResult("finalized", task=task, deleted_at=deleted_at, descendants=descendants)


async def finalize_closed_issue(
    db, guild_pk: int, guild_id: str, issue_repo: str, issue_number: int
) -> list[str]:
    """Clean up tasks linked to a GitHub issue that just closed.

    Shared by the periodic closed-issue sweep (``foreman.runner._sweep_closed_issues``)
    and the ``issues`` webhook handler (``routes.webhooks.github_webhook``). Legacy
    ``phase='issue'`` root rows are finalized through :func:`finalize_task`. Every
    already-terminal task linked to the issue gets its soft-delete stamped;
    non-terminal linked tasks are never force-closed (a human decides whether to
    cancel in-flight work) and only logged. Posts one pre-close verification comment
    summarising linked-PR merge status when anything was finalized. Returns the ids
    of the tasks that were finalized or swept.
    """
    issue_filter = (
        col(Task.guild_id) == guild_pk,
        col(Task.issue_repo) == issue_repo,
        col(Task.issue_number) == issue_number,
    )

    # Legacy phase='issue' roots: synthetic rows owned by the issue itself. The
    # terminal-state filter here is just to avoid pointless work — finalize_task's
    # conditional UPDATE is what actually makes a concurrent sweep safe.
    root_result = await db.exec(
        select(col(Task.id)).where(
            *issue_filter, col(Task.phase) == "issue", ~col(Task.state).in_(list(TERMINAL_STATES))
        )
    )
    finalized: list[str] = []
    for root_id in root_result.all():
        res = await finalize_task(
            db, guild_pk=guild_pk, guild_id=guild_id, task_id=root_id, outcome="done"
        )
        if res.finalized:
            finalized.append(root_id)

    linked_result = await db.exec(select(Task).where(*issue_filter))
    linked = list(linked_result.all())
    finalized += await cascade_soft_delete_terminal_descendants(db, linked, datetime.now(UTC))
    await db.commit()

    open_tasks = [t.id for t in linked if t.state not in TERMINAL_STATES and t.phase != "issue"]
    if open_tasks:
        logger.warning(
            "guild=%s issue %s#%s closed but %d non-terminal linked task(s) remain "
            "open: %s — leaving them as-is",
            guild_id,
            issue_repo,
            issue_number,
            len(open_tasks),
            ", ".join(open_tasks),
        )

    if finalized:
        # Imported here: the GitHub helpers live in foreman.tools, which imports
        # this module.
        from foreman.tools import post_issue_close_summary_comment  # noqa: PLC0415

        await post_issue_close_summary_comment(guild_id, issue_repo, issue_number, linked)
    return finalized
