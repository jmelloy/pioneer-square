"""Task lifecycle routes: list, logs, follow-up, finalize, cancel, redirect.

Soft-delete TTL handling lives here too — ``DEFAULT_FINALIZE_TTL``,
``FinalizeBody``, and ``_resolve_finalize_deleted_at`` are exported for
direct unit testing in ``tests/test_soft_delete_tasks.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from auth_deps import require_member
from database import get_db
from events import broadcast
from fastapi import APIRouter, Depends, HTTPException
from models import Guild, Task, TaskLog, live_tasks_filter
from sqlalchemy import select, update
from sqlmodel import SQLModel

router = APIRouter()


# Default soft-delete window for finalized tasks when the caller does not
# specify one. 3 days lets the operator review recent work in the UI before
# it disappears.
DEFAULT_FINALIZE_TTL = timedelta(days=3)


class FollowupCreate(SQLModel):
    instructions: str


class FinalizeBody(SQLModel):
    # Optional ISO-8601 timestamp at which to soft-delete this task.
    deleted_at: str | None = None
    # Optional convenience: seconds from now until soft-delete. If both fields
    # are set, deleted_at wins.
    expires_in_seconds: int | None = None


class RedirectCreate(SQLModel):
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
            .where(Task.guild_id == guild_id, live_tasks_filter())
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
        result = await db.execute(
            select(Task.id).where(
                Task.id == task_id,
                Task.guild_id == guild_id,
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
        result = await db.execute(select(Guild.id).where(Guild.id == guild_id))
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
            stmt = stmt.where(TaskLog.worker_id == worker_id)
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
    """Send follow-up instructions to a worker — executed in the same worktree/branch."""
    db = await get_db()
    try:
        result = await db.execute(
            select(Task.worker_id).where(Task.id == task_id, Task.guild_id == guild_id)
        )
        worker_id = result.scalar_one_or_none()
        if not worker_id:
            raise HTTPException(status_code=404, detail="Task not found")
        await db.execute(
            update(Task).where(Task.id == task_id).values(state="working", phase="followup")
        )
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "task-followup",
            "workerId": worker_id,
            "taskId": task_id,
            "instructions": data.instructions,
        },
    )
    return {"status": "sent", "taskId": task_id}


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
        result = await db.execute(
            select(Task.worker_id).where(Task.id == task_id, Task.guild_id == guild_id)
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
        result = await db.execute(
            select(Task.worker_id, Task.state).where(Task.id == task_id, Task.guild_id == guild_id)
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
            .values(state="cancelled", finished_at=finished_at)
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
        result = await db.execute(
            select(Task.worker_id, Task.state).where(Task.id == task_id, Task.guild_id == guild_id)
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
