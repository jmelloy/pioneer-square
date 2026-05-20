"""Task lifecycle routes: list, logs, follow-up, finalize, cancel, redirect.

Soft-delete TTL handling lives here too — ``DEFAULT_FINALIZE_TTL``,
``FinalizeBody``, and ``_resolve_finalize_deleted_at`` are exported for
direct unit testing in ``tests/test_soft_delete_tasks.py``.
"""

from __future__ import annotations

import json
import random
import string
from datetime import UTC, datetime, timedelta

from auth_deps import get_guild_pk, require_member
from database import get_db
from events import broadcast
from fastapi import APIRouter, Depends, HTTPException
from foreman.tools import _select_followup_worker
from models import Guild, Task, TaskLog, live_tasks_filter
from pydantic import BaseModel
from sqlalchemy import or_, select, update

router = APIRouter()


# Default soft-delete window for finalized tasks when the caller does not
# specify one. 3 days lets the operator review recent work in the UI before
# it disappears.
DEFAULT_FINALIZE_TTL = timedelta(days=3)


class FollowupCreate(BaseModel):
    instructions: str


class FinalizeBody(BaseModel):
    # Optional ISO-8601 timestamp at which to soft-delete this task.
    deleted_at: str | None = None
    # Optional convenience: seconds from now until soft-delete. If both fields
    # are set, deleted_at wins.
    expires_in_seconds: int | None = None


class RedirectCreate(BaseModel):
    instructions: str


def _resolve_finalize_deleted_at(body: FinalizeBody | None) -> str:
    """Return an ISO-8601 UTC timestamp for the task's soft-delete instant.

    Honours an explicit ``deleted_at`` first, then ``expires_in_seconds``,
    and otherwise falls back to ``now + DEFAULT_FINALIZE_TTL``.
    """
    if body and body.deleted_at:
        try:
            parsed = datetime.fromisoformat(body.deleted_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid deleted_at: {exc}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    if body and body.expires_in_seconds is not None:
        if body.expires_in_seconds < 0:
            raise HTTPException(status_code=400, detail="expires_in_seconds must be >= 0")
        return (datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)).isoformat()
    return (datetime.now(UTC) + DEFAULT_FINALIZE_TTL).isoformat()


@router.get("/guilds/{guild_id}/tasks")
async def list_guild_tasks(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
):
    """List all tasks for a guild, most recent first."""
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(
                Task.id,
                Task.worker_id,
                Task.name,
                Task.description,
                Task.tool,
                Task.state,
                Task.phase,
                Task.parent_task_id,
                Task.branch,
                Task.pr_url,
                Task.issue_number,
                Task.issue_repo,
                Task.created_at,
                Task.finished_at,
                Task.deleted_at,
            )
            .where(Task.guild_pk == guild_pk, live_tasks_filter())
            .order_by(Task.created_at.desc())
            .limit(100)
        )
        return [dict(r._mapping) for r in result.fetchall()]
    finally:
        await db.close()


@router.get("/guilds/{guild_id}/tasks/{task_id}/logs")
async def get_task_logs(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
):
    """Get all saved log lines for a task."""
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Task.id).where(
                Task.id == task_id,
                Task.guild_pk == guild_pk,
                live_tasks_filter(),
            )
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Task not found")
        result = await db.execute(
            select(
                TaskLog.timestamp, TaskLog.line, TaskLog.worker_id, TaskLog.agent_id, TaskLog.data
            )
            .where(TaskLog.task_id == task_id)
            .order_by(TaskLog.id.asc())
        )
        rows = []
        for r in result.fetchall():
            row = dict(r._mapping)
            raw = row.pop("data", None)
            if raw:
                try:
                    row["detail"] = json.loads(raw)
                except Exception:
                    pass
            rows.append(row)
        return rows
    finally:
        await db.close()


@router.get("/guilds/{guild_id}/logs")
async def get_guild_logs(
    guild_id: str,
    worker_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    github_user_id: str = Depends(require_member()),
):
    """Get task_logs filtered by worker_id, agent_id, or task_id."""
    if not (worker_id or agent_id or task_id):
        raise HTTPException(status_code=400, detail="Specify worker_id, agent_id, or task_id")
    db = await get_db()
    try:
        result = await db.execute(select(Guild.guild_id).where(Guild.guild_id == guild_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Guild not found")
        stmt = select(
            TaskLog.timestamp,
            TaskLog.line,
            TaskLog.worker_id,
            TaskLog.agent_id,
            TaskLog.task_id,
            TaskLog.data,
        )
        if task_id:
            stmt = stmt.where(TaskLog.task_id == task_id)
        elif worker_id:
            stmt = stmt.where(TaskLog.worker_id == worker_id, TaskLog.task_id.is_(None))
        else:
            stmt = stmt.where(TaskLog.agent_id == agent_id)
        stmt = stmt.order_by(TaskLog.id.asc())
        result = await db.execute(stmt)
        rows = []
        for r in result.fetchall():
            row = dict(r._mapping)
            raw = row.pop("data", None)
            if raw:
                try:
                    row["detail"] = json.loads(raw)
                except Exception:
                    pass
            rows.append(row)
        return rows
    finally:
        await db.close()


@router.post("/guilds/{guild_id}/tasks/{task_id}/followup")
async def create_task_followup(
    guild_id: str,
    task_id: str,
    data: FollowupCreate,
    github_user_id: str = Depends(require_member()),
):
    """Dispatch a user-initiated follow-up directly, bypassing the task_events queue.

    User clicks are immediate — they should never be silently deferred to the
    task_events debounce queue (which is reserved for webhook-driven events).
    Instead this route acquires the follow-up lock and dispatches inline, or
    returns 409 if the task is already processing a follow-up.
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(
                Task.worker_id,
                Task.state,
                Task.branch,
                Task.description,
                Task.name,
                Task.tool,
                Task.issue_number,
                Task.issue_repo,
            ).where(Task.id == task_id, Task.guild_pk == guild_pk)
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        (
            original_worker_id,
            prior_state,
            branch,
            task_desc,
            task_name,
            task_tool,
            task_issue_number,
            task_issue_repo,
        ) = row

        if not branch:
            raise HTTPException(
                status_code=400,
                detail="Task has no branch recorded — cannot dispatch a follow-up.",
            )

        target_worker_id = await _select_followup_worker(
            db,
            guild_id=guild_id,
            guild_pk=guild_pk,
            original_worker_id=original_worker_id,
        )
        if not target_worker_id:
            raise HTTPException(
                status_code=503,
                detail="No idle worker available. Wait for one to come online and try again.",
            )

        # Atomically acquire the follow-up lock — same mechanism as send_followup in tools.py.
        # Stale locks (older than 1 hour) are overridden automatically.
        lock_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        now_ts = datetime.now(UTC).isoformat()
        stale_cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        lock_result = await db.execute(
            update(Task)
            .where(
                Task.id == task_id,
                or_(Task.locked_at.is_(None), Task.locked_at < stale_cutoff),
            )
            .values(locked_at=now_ts, lock_holder=lock_id)
        )
        await db.commit()

        if lock_result.rowcount == 0:
            # Task is locked by a concurrent follow-up already in flight.
            # Unlike webhook-driven events we do NOT queue — surface the conflict
            # to the user so they can retry once the current follow-up finishes.
            raise HTTPException(
                status_code=409,
                detail="Task is currently processing a follow-up. Please wait and try again.",
            )

        update_vals: dict = {
            "state": "working",
            "phase": "followup",
            "worker_id": target_worker_id,
        }
        if prior_state in ("done", "failed", "cancelled"):
            update_vals["deleted_at"] = None
            update_vals["finished_at"] = None
        await db.execute(update(Task).where(Task.id == task_id).values(**update_vals))
        await db.commit()

        await broadcast(
            guild_id,
            {
                "type": "task-update",
                "taskId": task_id,
                "state": "working",
                "workerId": target_worker_id,
                "deletedAt": None,
                "finishedAt": None,
            },
        )
        await broadcast(
            guild_id,
            {
                "type": "task-followup",
                "workerId": target_worker_id,
                "taskId": task_id,
                "name": task_name or "",
                "description": task_desc or "",
                "tool": task_tool or "claude",
                "branch": branch,
                "instructions": data.instructions,
                "issueNumber": task_issue_number,
                "issueRepo": task_issue_repo,
            },
        )
        return {"status": "dispatched", "taskId": task_id, "workerId": target_worker_id}
    finally:
        await db.close()


@router.post("/guilds/{guild_id}/tasks/{task_id}/finalize")
async def finalize_task_endpoint(
    guild_id: str,
    task_id: str,
    body: FinalizeBody | None = None,
    github_user_id: str = Depends(require_member()),
):
    """Signal a worker to finalize a task — no more follow-ups.

    The optional body may carry ``deleted_at`` (ISO-8601) or
    ``expires_in_seconds`` to set the task's soft-delete window. If neither is
    set, the task is soft-deleted ``DEFAULT_FINALIZE_TTL`` from now.
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Task.worker_id).where(Task.id == task_id, Task.guild_pk == guild_pk)
        )
        worker_id = result.scalar_one_or_none()
        if not worker_id:
            raise HTTPException(status_code=404, detail="Task not found")
        finished_at = datetime.now(UTC).isoformat()
        deleted_at = _resolve_finalize_deleted_at(body)
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(state="done", finished_at=finished_at, deleted_at=deleted_at)
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "task-finalize",
            "workerId": worker_id,
            "taskId": task_id,
        },
    )
    await broadcast(
        guild_id,
        {
            "type": "task-update",
            "taskId": task_id,
            "state": "done",
            "finishedAt": finished_at,
            "deletedAt": deleted_at,
        },
    )
    return {"status": "finalized", "taskId": task_id, "deletedAt": deleted_at}


@router.post("/guilds/{guild_id}/tasks/{task_id}/cancel")
async def cancel_task_endpoint(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
):
    """Cancel a running or pending task — terminates the worker's Claude subprocess."""
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Task.worker_id, Task.state).where(Task.id == task_id, Task.guild_pk == guild_pk)
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        worker_id, state = row
        if state in ("done", "failed", "cancelled"):
            raise HTTPException(status_code=409, detail=f"Task is already {state}")
        finished_at = datetime.now(UTC).isoformat()
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(state="cancelled", finished_at=finished_at, locked_at=None, lock_holder=None)
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "task-cancel",
            "workerId": worker_id,
            "taskId": task_id,
        },
    )
    await broadcast(
        guild_id,
        {
            "type": "task-update",
            "taskId": task_id,
            "state": "cancelled",
            "finishedAt": finished_at,
        },
    )
    return {"status": "cancelled", "taskId": task_id}


@router.post("/guilds/{guild_id}/tasks/{task_id}/redirect")
async def redirect_task_endpoint(
    guild_id: str,
    task_id: str,
    data: RedirectCreate,
    github_user_id: str = Depends(require_member()),
):
    """Redirect a running task: SIGTERM the Claude subprocess and resume it with new instructions."""
    instructions = data.instructions.strip()
    if not instructions:
        raise HTTPException(status_code=400, detail="instructions required")
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Task.worker_id, Task.state).where(Task.id == task_id, Task.guild_pk == guild_pk)
        )
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        worker_id, state = row
        if state in ("done", "failed", "cancelled"):
            raise HTTPException(status_code=409, detail=f"Task is already {state}")
        await db.execute(update(Task).where(Task.id == task_id).values(state="working"))
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "task-redirect",
            "workerId": worker_id,
            "taskId": task_id,
            "instructions": instructions,
        },
    )
    await broadcast(
        guild_id,
        {
            "type": "task-update",
            "taskId": task_id,
            "state": "working",
        },
    )
    return {"status": "redirected", "taskId": task_id}
