"""Task lifecycle routes: list, logs, follow-up, finalize, cancel, redirect.

Soft-delete is a single ``deleted_at`` stamp (see ``models.finalize_soft_delete_at``
/ ``live_tasks_filter``) — no per-task TTL windows.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime

from auth_deps import ensure_membership, get_guild_pk, require_member, require_user
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from foreman import triggers
from lock_service import LockService
from models import (
    Conversation,
    Guild,
    Task,
    TaskLog,
    live_tasks_filter,
)
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from task_lifecycle import TERMINAL_STATES, finalize_task
from ws_types import (
    TaskCancelMsg,
    TaskRedirectMsg,
    TaskUpdateMsg,
    WorkerMessageMsg,
)

router = APIRouter()


class FollowupCreate(BaseModel):
    instructions: str


class RedirectCreate(BaseModel):
    instructions: str


class TaskMessageCreate(BaseModel):
    message: str


async def _fetch_log_rows(db: AsyncSession, task_id: str) -> list[dict]:
    """Return every saved log line for *task_id*, oldest first, with `data` decoded."""
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


@router.get("/guilds/{guild_id}/tasks")
async def list_guild_tasks(
    guild_id: str,
    issue_number: int | None = None,
    issue_repo: str | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List all tasks for a guild, most recent first.

    Optionally filter by ``issue_number`` and/or ``issue_repo`` to return only
    tasks linked to a specific GitHub issue.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    stmt = (
        select(
            col(Task.id),
            col(Task.worker_id),
            col(Task.name),
            col(Task.description),
            col(Task.tool),
            col(Task.task_type),
            col(Task.state),
            col(Task.phase),
            col(Task.parent_task_id),
            col(Task.branch),
            col(Task.pr_url),
            col(Task.issue_number),
            col(Task.issue_repo),
            col(Task.conversation_id),
            col(Task.created_at),
            col(Task.deleted_at),
        )
        .where(col(Task.guild_id) == guild_pk, live_tasks_filter())
        .order_by(col(Task.created_at).desc())
    )
    if issue_number is not None:
        stmt = stmt.where(col(Task.issue_number) == issue_number)
    if issue_repo is not None:
        stmt = stmt.where(col(Task.issue_repo) == issue_repo)
    stmt = stmt.limit(100)
    result = await db.exec(stmt)
    return [dict(r._mapping) for r in result.all()]


@router.get("/guilds/{guild_id}/tasks/{task_id}/logs")
async def get_task_logs(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Get saved log lines, including archived tasks, for guild members."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Task.id)).where(
            col(Task.id) == task_id,
            col(Task.guild_id) == guild_pk,
        )
    )
    if not result.one_or_none():
        raise HTTPException(status_code=404, detail="Task not found")
    return await _fetch_log_rows(db, task_id)


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

    await triggers.trigger_foreman(
        guild_id,
        "user-followup",
        triggers.format_user_followup_message(task_id, state, branch, data.instructions),
        user_id=github_user_id,
        task_id=task_id,
        task_name=f"foreman.user-followup:{task_id}",
    )
    return {"status": "queued_for_foreman", "taskId": task_id}


@router.post("/guilds/{guild_id}/tasks/{task_id}/finalize")
async def finalize_task_endpoint(
    guild_id: str,
    task_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Finalize a task from the UI — no more follow-ups.

    Delegates to ``task_lifecycle.finalize_task``, the same TOCTOU-safe path the
    foreman tool and the PR webhooks use: the task lock is released, queued
    follow-up events are discarded, and a ``phase='issue'`` root cascades its
    soft-delete to already-finished descendants.

    Soft-deletes the task now, unless it is tied to a still-open issue: then
    ``deleted_at`` stays NULL until the issue closes (see
    ``models.finalize_soft_delete_at``).
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    res = await finalize_task(
        db, guild_pk=guild_pk, guild_id=guild_id, task_id=task_id, outcome="done"
    )
    if res.status == "not_found":
        raise HTTPException(status_code=404, detail="Task not found")
    if res.status == "already_terminal":
        raise HTTPException(status_code=409, detail=f"Task is already {res.task.state}")
    # Return raw datetime — FastAPI's jsonable_encoder handles ISO-8601 serialisation.
    return {"status": "finalized", "taskId": task_id, "deletedAt": res.deleted_at}


@router.post("/guilds/{guild_id}/tasks/{task_id}/message")
async def message_task_endpoint(
    guild_id: str,
    task_id: str,
    data: TaskMessageCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Inject a message into a running interactive task."""
    text_msg = data.message.strip()
    if not text_msg:
        raise HTTPException(status_code=400, detail="Empty message")
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    row = (
        await db.exec(
            select(col(Task.worker_id), col(Task.state)).where(
                col(Task.id) == task_id, col(Task.guild_id) == guild_pk
            )
        )
    ).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    worker_id, state = row
    if not worker_id:
        raise HTTPException(status_code=400, detail="Task is not assigned to a worker")
    if state != "working":
        raise HTTPException(status_code=409, detail=f"Task is {state}, not working")
    await broadcast_msg(
        guild_id, WorkerMessageMsg(workerId=worker_id, message=text_msg, taskId=task_id)
    )
    return {"status": "sent", "taskId": task_id}


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
    if state in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail=f"Task is already {state}")
    deleted_at = datetime.now(UTC)
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


# ---------------------------------------------------------------------------
# Task tree endpoint helpers
# ---------------------------------------------------------------------------


def _nest_tasks(tasks: list[dict]) -> list[dict]:
    """Build a parent/child tree from a flat task list using parent_task_id.

    A ``phase='issue'`` task is the canonical root of its issue-rooted subtree.
    If it has no parent itself, any other parentless task in the same list
    (e.g. a plan/execute task whose parent_task_id wasn't set) is nested under
    it instead of surfacing as a second top-level root.
    """
    by_id = {t["id"]: dict(t, children=[]) for t in tasks}
    roots: list[dict] = []
    for t in by_id.values():
        parent_id = t.get("parent_task_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(t)
        else:
            roots.append(t)

    issue_roots = [t for t in roots if t.get("phase") == "issue"]
    if len(issue_roots) == 1:
        issue_root = issue_roots[0]
        other_roots = [t for t in roots if t is not issue_root]
        if other_roots:
            issue_root["children"].extend(other_roots)
            roots = [issue_root]

    return roots


@router.get("/guilds/{guild_id}/tasks/tree")
async def get_guild_tasks_tree(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return tasks grouped by conversation.

    Each task's ``conversation_id`` is propagated through parent_task_id links
    in both directions so a task without a conversation joins its relatives'
    conversation. Tasks that still have none are returned in ``ungrouped``.
    Conversations with active status and non-terminal tasks sort first, then
    by most recent activity.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    result = await db.exec(
        select(
            col(Task.id),
            col(Task.worker_id),
            col(Task.name),
            col(Task.description),
            col(Task.phase),
            col(Task.state),
            col(Task.parent_task_id),
            col(Task.branch),
            col(Task.pr_url),
            col(Task.pr_number),
            col(Task.pr_repo),
            col(Task.issue_number),
            col(Task.issue_repo),
            col(Task.issue_state),
            col(Task.issue_title),
            col(Task.conversation_id),
            col(Task.created_at),
            col(Task.deleted_at),
        )
        .where(col(Task.guild_id) == guild_pk, live_tasks_filter())
        .order_by(col(Task.created_at).asc())
        .limit(200)
    )
    raw_tasks = [dict(r._mapping) for r in result.all()]

    # --- Propagate conversation_id through parent links, both directions.
    # Values never change once set, so each pass assigns at least one new
    # task and the loop terminates. ---
    task_conv: dict[str, int] = {
        t["id"]: t["conversation_id"] for t in raw_tasks if t["conversation_id"] is not None
    }
    ids = {t["id"] for t in raw_tasks}
    links = [(t["id"], t["parent_task_id"]) for t in raw_tasks if t["parent_task_id"] in ids]
    changed = True
    while changed:
        changed = False
        for tid, pid in links:
            if tid in task_conv and pid not in task_conv:
                task_conv[pid] = task_conv[tid]
                changed = True
            elif pid in task_conv and tid not in task_conv:
                task_conv[tid] = task_conv[pid]
                changed = True

    groups: dict[int, list[dict]] = defaultdict(list)
    ungrouped: list[dict] = []
    for task in raw_tasks:
        conv_id = task_conv.get(task["id"])
        if conv_id is not None:
            task["conversation_id"] = conv_id
            groups[conv_id].append(task)
        else:
            ungrouped.append(task)

    convs: dict[int, Conversation] = {}
    if groups:
        conv_result = await db.exec(
            select(Conversation).where(
                col(Conversation.id).in_(groups.keys()),
                col(Conversation.guild_id) == guild_pk,
            )
        )
        convs = {c.id: c for c in conv_result.all() if c.id is not None}

    entries = []
    for conv_id, group_tasks in groups.items():
        conv = convs.get(conv_id)
        if conv is None:
            # Conversation missing or in another guild: don't leak it.
            for t in group_tasks:
                t["conversation_id"] = None
            ungrouped.extend(group_tasks)
            continue
        has_live = any(t["state"] not in TERMINAL_STATES for t in group_tasks)
        activity = conv.updated_at or conv.created_at
        entries.append(
            (
                conv.status == "active" and has_live,
                activity,
                {
                    "id": conv.id,
                    "name": conv.name,
                    "status": conv.status,
                    "created_at": conv.created_at,
                    "updated_at": conv.updated_at,
                    "tasks": _nest_tasks(group_tasks),
                },
            )
        )

    # Active-with-live-tasks first, then most recent activity first.
    entries.sort(key=lambda e: e[1], reverse=True)
    entries.sort(key=lambda e: not e[0])
    ungrouped.sort(key=lambda t: t["created_at"])

    return {"conversations": [e[2] for e in entries], "ungrouped": _nest_tasks(ungrouped)}


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
    if state in TERMINAL_STATES:
        raise HTTPException(status_code=409, detail=f"Task is already {state}")
    await db.exec(update(Task).where(col(Task.id) == task_id).values(state="working"))
    await db.commit()
    await broadcast_msg(
        guild_id, TaskRedirectMsg(workerId=worker_id, taskId=task_id, instructions=instructions)
    )
    await broadcast_msg(guild_id, TaskUpdateMsg(taskId=task_id, state="working"))
    return {"status": "redirected", "taskId": task_id}


@router.get("/api/task/{task_id}/log")
async def get_task_log_page(
    task_id: str,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Task metadata + full log output for the shareable ``/task/{id}/log`` view.

    Guild-less on purpose: task ids are globally unique, so a commit message can
    link straight to ``/task/t-abc123/log`` without knowing the guild. Membership
    is still enforced — the guild is resolved from the task, then checked.
    Read-only; archival hides tasks from live lists, not from stable log links.
    """
    row = (
        await db.exec(
            select(Task, col(Guild.slug))
            .join(Guild, col(Task.guild_id) == col(Guild.id))
            .where(col(Task.id) == task_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task, guild_slug = row
    await ensure_membership(db, guild_slug, github_user_id)

    logs = await _fetch_log_rows(db, task_id)
    # tasks has no updated_at column; the newest log line is the best proxy for
    # "last activity" and is what the viewer shows.
    updated_at = logs[-1]["timestamp"] if logs else task.created_at

    return {
        "task": {
            "id": task.id,
            "name": task.name,
            "description": task.description,
            "guild_id": guild_slug,
            "worker_id": task.worker_id,
            "state": task.state,
            "phase": task.phase,
            "tool": task.tool,
            "model": task.model,
            "branch": task.branch,
            "created_at": task.created_at,
            "updated_at": updated_at,
            "issue_number": task.issue_number,
            "issue_repo": task.issue_repo,
            "issue_title": task.issue_title,
            "pr_url": task.pr_url,
        },
        "logs": logs,
    }
