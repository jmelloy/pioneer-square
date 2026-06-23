"""Task lifecycle routes: list, logs, follow-up, finalize, cancel, redirect.

Soft-delete TTL handling lives here too — ``DEFAULT_FINALIZE_TTL``,
``FinalizeBody``, and ``_resolve_finalize_deleted_at`` are exported for
direct unit testing in ``tests/test_soft_delete_tasks.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from lock_service import LockService
from models import Guild, Task, TaskLog, live_tasks_filter
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from ws_types import TaskCancelMsg, TaskFinalizeMsg, TaskRedirectMsg, TaskUpdateMsg

from foreman import run_foreman_ai

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


def _resolve_finalize_deleted_at(body: FinalizeBody | None) -> datetime:
    """Return a UTC datetime for the task's soft-delete instant."""
    if body and body.deleted_at:
        try:
            parsed = datetime.fromisoformat(body.deleted_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid deleted_at: {exc}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if body and body.expires_in_seconds is not None:
        if body.expires_in_seconds < 0:
            raise HTTPException(status_code=400, detail="expires_in_seconds must be >= 0")
        return datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
    return datetime.now(UTC) + DEFAULT_FINALIZE_TTL


@router.get("/guilds/{guild_id}/tasks")
async def list_guild_tasks(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List all tasks for a guild, most recent first."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(
            col(Task.id),
            col(Task.worker_id),
            col(Task.name),
            col(Task.description),
            col(Task.tool),
            col(Task.state),
            col(Task.phase),
            col(Task.parent_task_id),
            col(Task.branch),
            col(Task.pr_url),
            col(Task.issue_number),
            col(Task.issue_repo),
            col(Task.created_at),
            col(Task.deleted_at),
        )
        .where(col(Task.guild_id) == guild_pk, live_tasks_filter())
        .order_by(col(Task.created_at).desc())
        .limit(100)
    )
    return [dict(r._mapping) for r in result.all()]


@router.get("/guilds/{guild_id}/tasks/{task_id}/logs")
async def get_task_logs(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Get all saved log lines for a task."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.id)).where(
            col(Task.id) == task_id,
            col(Task.guild_id) == guild_pk,
            live_tasks_filter(),
        )
    )
    if not result.one_or_none():
        raise HTTPException(status_code=404, detail="Task not found")
    result = await db.exec(
        select(
            col(TaskLog.timestamp),
            col(TaskLog.line),
            col(TaskLog.worker_id),
            col(TaskLog.agent_id),
            col(TaskLog.data),
            col(TaskLog.level),
        )
        .where(col(TaskLog.task_id) == task_id)
        .order_by(col(TaskLog.id).asc())
    )
    rows = []
    for r in result.all():
        row = dict(r._mapping)
        raw = row.pop("data", None)
        if raw:
            try:
                row["detail"] = json.loads(raw)
            except Exception:
                pass
        rows.append(row)
    return rows


@router.get("/guilds/{guild_id}/logs")
async def get_guild_logs(
    guild_id: str,
    worker_id: str | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Get task_logs filtered by worker_id, agent_id, or task_id."""
    if not (worker_id or agent_id or task_id):
        raise HTTPException(status_code=400, detail="Specify worker_id, agent_id, or task_id")
    result = await db.exec(select(col(Guild.slug)).where(col(Guild.slug) == guild_id))
    if not result.one_or_none():
        raise HTTPException(status_code=404, detail="Guild not found")
    stmt = select(
        col(TaskLog.timestamp),
        col(TaskLog.line),
        col(TaskLog.worker_id),
        col(TaskLog.agent_id),
        col(TaskLog.task_id),
        col(TaskLog.data),
        col(TaskLog.level),
    )
    if task_id:
        stmt = stmt.where(col(TaskLog.task_id) == task_id)
    elif worker_id:
        stmt = stmt.where(col(TaskLog.worker_id) == worker_id, col(TaskLog.task_id).is_(None))
    else:
        stmt = stmt.where(col(TaskLog.agent_id) == agent_id)
    stmt = stmt.order_by(col(TaskLog.id).asc())
    result = await db.exec(stmt)
    rows = []
    for r in result.all():
        row = dict(r._mapping)
        raw = row.pop("data", None)
        if raw:
            try:
                row["detail"] = json.loads(raw)
            except Exception:
                pass
        rows.append(row)
    return rows


@router.post("/guilds/{guild_id}/tasks/{task_id}/followup")
async def create_task_followup(
    guild_id: str,
    task_id: str,
    data: FollowupCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Route a user-supplied follow-up to the foreman.

    Workers no longer hold tasks open for follow-ups — the foreman owns task
    lifecycle and decides which idle worker resumes the branch (the original
    worker reuses its worktree if it's still idle; otherwise any idle worker
    pulls the branch from GitHub). All follow-ups go through the foreman so
    the same routing logic applies in every case.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id), col(Task.state), col(Task.branch)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    _worker_id, state, branch = row

    branch_ctx = f" on branch `{branch}`" if branch else ""
    spawn(
        run_foreman_ai(
            guild_id,
            f"[user-followup] User requested follow-up on task {task_id}{branch_ctx} "
            f'(currently {state}): "{data.instructions}". '
            "Call send_followup to dispatch this work — it will pick the "
            "original worker if idle, otherwise any idle worker pulls the "
            "branch from GitHub.",
            user_id=github_user_id,
            task_id=task_id,
        ),
        name=f"foreman.user-followup:{task_id}",
    )
    return {"status": "queued_for_foreman", "taskId": task_id}


@router.post("/guilds/{guild_id}/tasks/{task_id}/finalize")
async def finalize_task_endpoint(
    guild_id: str,
    task_id: str,
    body: FinalizeBody | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Signal a worker to finalize a task — no more follow-ups.

    The optional body may carry ``deleted_at`` (ISO-8601) or
    ``expires_in_seconds`` to set the task's soft-delete window. If neither is
    set, the task is soft-deleted ``DEFAULT_FINALIZE_TTL`` from now.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id)).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
    )
    worker_id = result.one_or_none()
    if not worker_id:
        raise HTTPException(status_code=404, detail="Task not found")
    deleted_at = _resolve_finalize_deleted_at(body)
    await db.exec(
        update(Task).where(col(Task.id) == task_id).values(state="done", deleted_at=deleted_at)
    )
    await db.commit()
    await broadcast_msg(guild_id, TaskFinalizeMsg(workerId=worker_id, taskId=task_id))
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(
            taskId=task_id,
            state="done",
            deletedAt=deleted_at.isoformat(),
        ),
    )
    # Return raw datetime — FastAPI's jsonable_encoder handles ISO-8601 serialisation.
    return {"status": "finalized", "taskId": task_id, "deletedAt": deleted_at}


@router.post("/guilds/{guild_id}/tasks/{task_id}/cancel")
async def cancel_task_endpoint(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Cancel a running or pending task — terminates the worker's Claude subprocess."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id), col(Task.state)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    worker_id, state = row
    if state in ("done", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Task is already {state}")
    deleted_at = datetime.now(UTC) + DEFAULT_FINALIZE_TTL
    await db.exec(
        update(Task).where(col(Task.id) == task_id).values(state="cancelled", deleted_at=deleted_at)
    )
    await LockService(db).release(f"task:{task_id}")
    await db.commit()
    await broadcast_msg(guild_id, TaskCancelMsg(workerId=worker_id, taskId=task_id))
    await broadcast_msg(
        guild_id,
        TaskUpdateMsg(taskId=task_id, state="cancelled", deletedAt=deleted_at.isoformat()),
    )
    return {"status": "cancelled", "taskId": task_id}


@router.post("/guilds/{guild_id}/tasks/{task_id}/redirect")
async def redirect_task_endpoint(
    guild_id: str,
    task_id: str,
    data: RedirectCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Redirect a running task: SIGTERM the Claude subprocess and resume it with new instructions."""
    instructions = data.instructions.strip()
    if not instructions:
        raise HTTPException(status_code=400, detail="instructions required")
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.worker_id), col(Task.state)).where(
            col(Task.id) == task_id, col(Task.guild_id) == guild_pk
        )
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    worker_id, state = row
    if state in ("done", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Task is already {state}")
    await db.exec(update(Task).where(col(Task.id) == task_id).values(state="working"))
    await db.commit()
    await broadcast_msg(
        guild_id, TaskRedirectMsg(workerId=worker_id, taskId=task_id, instructions=instructions)
    )
    await broadcast_msg(guild_id, TaskUpdateMsg(taskId=task_id, state="working"))
    return {"status": "redirected", "taskId": task_id}
