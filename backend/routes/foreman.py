"""Foreman REST API — conversation history (existing debug endpoints) and the
new Phase 1 endpoints that let an external standalone foreman process read
guild state and write task/message mutations without direct DB access.

Auth: all new endpoints accept either a worker auth_token *or* a member
login_token via ``require_worker_or_member_path``. This matches the existing
worker credential endpoints and keeps the door open for the standalone
foreman to register with a worker-style auth_token in Phase 3.

Endpoint summary
----------------
Existing (unchanged):
  GET  /guilds/{guild_id}/foreman/context        — debug: stored turns for calling user
  POST /guilds/{guild_id}/foreman/clear-context  — debug: delete all turns for calling user

Phase 1 additions — state reads:
  GET  /guilds/{guild_id}/foreman/state          — online workers, active tasks, guild metadata
  GET  /guilds/{guild_id}/foreman/history        — raw ForemanTurn rows for a given user_id
  GET  /guilds/{guild_id}/guild-key              — Ed25519 private key PEM for JWT signing

Phase 1 additions — state writes:
  POST  /guilds/{guild_id}/foreman/history       — persist one ForemanTurn row
  POST  /guilds/{guild_id}/tasks                 — create a foreman-owned task
  PATCH /guilds/{guild_id}/tasks/{task_id}       — update task fields (state, worker, branch, …)
  POST  /guilds/{guild_id}/messages              — persist a chat message
"""

from __future__ import annotations

import json
import logging
import random
import string
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member, require_worker_or_member_path
from database import get_db
from events import broadcast
from fastapi import APIRouter, Depends, HTTPException, Query
from models import (
    ForemanTurn,
    Guild,
    GuildKey,
    Message,
    Task,
    live_tasks_filter,
)
from pydantic import BaseModel
from sqlalchemy import select, text, update

from foreman import clear_foreman_history, get_foreman_history

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Existing debug endpoints (unchanged)
# ---------------------------------------------------------------------------


@router.get("/guilds/{guild_id}/foreman/context")
async def get_foreman_context(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
):
    """Return the stored foreman conversation turns for this guild+user (debug view)."""
    db = await get_db()
    try:
        result = await db.execute(select(Guild.guild_id).where(Guild.guild_id == guild_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Guild not found")
    finally:
        await db.close()
    history = await get_foreman_history(guild_id, github_user_id)
    return {
        "system": history["system"],
        "messages": history["messages"],
        "count": len(history["messages"]),
    }


@router.post("/guilds/{guild_id}/foreman/clear-context")
async def clear_foreman_context(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
):
    """Delete all stored foreman turns for this guild+user. Chat history in messages table is preserved."""
    db = await get_db()
    try:
        result = await db.execute(select(Guild.guild_id).where(Guild.guild_id == guild_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Guild not found")
    finally:
        await db.close()
    removed = await clear_foreman_history(guild_id, github_user_id)
    logger.info(
        "Foreman context cleared for guild %s user %s (%d turns removed)",
        guild_id,
        github_user_id,
        removed,
    )
    return {"status": "cleared", "removed": removed}


# ---------------------------------------------------------------------------
# Phase 1 — State reads
# ---------------------------------------------------------------------------


@router.get("/guilds/{guild_id}/foreman/state")
async def get_foreman_state(
    guild_id: str,
    _caller: str = Depends(require_worker_or_member_path),
):
    """Return a snapshot of guild state for an external foreman process.

    Response shape::

        {
          "guild": { "name": str, "primary_repo": str | null },
          "workers": [
            {
              "id": str,
              "state": str,          # "online" | "offline"
              "repos": list[str],
              "org": str | null,
              "agent_count": int,
              "agents": str          # "agentId:state,…" summary
            }
          ],
          "tasks": [
            {
              "id": str, "worker_id": str, "name": str, "description": str,
              "state": str, "phase": str, "branch": str | null,
              "pr_url": str | null, "finished_at": str | null
            }
          ]
        }

    ``workers`` lists only online workers (state == 'online'). ``tasks``
    lists all non-terminal, non-soft-deleted tasks, most recent first.
    Mirrors the state snapshot built by ``run_foreman_ai()`` so the
    standalone foreman can construct the same system-prompt preamble.
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")

        # Guild metadata
        guild_res = await db.execute(
            select(Guild.name, Guild.primary_repo).where(Guild.guild_id == guild_id)
        )
        guild_row = guild_res.one_or_none()
        guild_data = {
            "name": guild_row.name if guild_row else None,
            "primary_repo": guild_row.primary_repo if guild_row else None,
        }

        # Online workers with their active agents
        workers_res = await db.execute(
            text(
                "SELECT w.id, w.repos, w.org, w.state as worker_state,"
                " COUNT(a.id) as agent_count,"
                " STRING_AGG(a.id || ':' || a.state, ',') as agents"
                " FROM workers w"
                " LEFT JOIN agents a ON a.worker_id = w.id AND a.state != 'offline'"
                " WHERE w.guild_pk = :guild_pk AND w.state = 'online'"
                " GROUP BY w.id"
                " UNION ALL"
                " SELECT a.id, '[]', NULL, a.state, 1, a.id || ':' || a.state"
                " FROM agents a"
                " WHERE a.guild_pk = :guild_pk AND a.type = 'worker'"
                " AND a.worker_id IS NULL AND a.state != 'offline'"
            ),
            {"guild_pk": guild_pk},
        )
        worker_rows = [dict(r._mapping) for r in workers_res.fetchall()]
        workers_data = [
            {
                "id": r["id"],
                "state": r["worker_state"] or "idle",
                "repos": json.loads(r["repos"] or "[]"),
                **({"org": r["org"]} if r.get("org") else {}),
                "agent_count": r["agent_count"] or 0,
                "agents": r["agents"] or "",
            }
            for r in worker_rows
        ]

        # Active (non-terminal, non-soft-deleted) tasks
        _TERMINAL = {"done", "failed", "cancelled"}
        tasks_res = await db.execute(
            select(
                Task.id,
                Task.worker_id,
                Task.name,
                Task.description,
                Task.state,
                Task.phase,
                Task.branch,
                Task.pr_url,
                Task.finished_at,
                Task.created_at,
                Task.user_id,
            )
            .where(
                Task.guild_pk == guild_pk,
                ~Task.state.in_(list(_TERMINAL)),
                live_tasks_filter(),
            )
            .order_by(Task.created_at.desc())
        )
        tasks_data = [dict(r._mapping) for r in tasks_res.fetchall()]
    finally:
        await db.close()

    return {"guild": guild_data, "workers": workers_data, "tasks": tasks_data}


@router.get("/guilds/{guild_id}/foreman/history")
async def get_foreman_history_for_user(
    guild_id: str,
    user_id: str = Query(..., description="GitHub user_id whose thread to fetch"),
    limit: int | None = Query(default=None, description="Max rows to return (newest first)"),
    _caller: str = Depends(require_worker_or_member_path),
):
    """Return raw ForemanTurn rows for a given user, ordered oldest→newest.

    Response shape::

        [
          {
            "id": int,
            "role": str,              # "user" | "assistant" | "system"
            "content_json": str,      # JSON-serialised content blocks
            "is_tool_response": bool,
            "parent_id": int | null,
            "created_at": str
          }
        ]

    The standalone foreman applies its own sliding-window logic (equivalent
    to ``_load_history()``) on top of these raw rows. ``limit`` caps the
    number of rows returned (applied before the sliding window).
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")

        stmt = (
            select(
                ForemanTurn.id,
                ForemanTurn.role,
                ForemanTurn.content_json,
                ForemanTurn.is_tool_response,
                ForemanTurn.parent_id,
                ForemanTurn.created_at,
            )
            .where(ForemanTurn.guild_pk == guild_pk, ForemanTurn.user_id == user_id)
            .order_by(ForemanTurn.id)
        )
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        turns = result.fetchall()
    finally:
        await db.close()

    return [
        {
            "id": t.id,
            "role": t.role,
            "content_json": t.content_json,
            "is_tool_response": bool(t.is_tool_response),
            "parent_id": t.parent_id,
            "created_at": t.created_at,
        }
        for t in turns
    ]


@router.get("/guilds/{guild_id}/guild-key")
async def get_guild_key(
    guild_id: str,
    _caller: str = Depends(require_worker_or_member_path),
):
    """Return the guild's Ed25519 signing key for JWT operations.

    Response shape::

        { "key_id": str, "private_key_pem": str }

    Used by the standalone foreman's ``dnsid sign`` tool to create JWTs
    without direct DB access. Returns 404 if no key has been generated yet.
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(GuildKey.key_id, GuildKey.private_key_pem).where(GuildKey.guild_pk == guild_pk)
        )
        row = result.one_or_none()
    finally:
        await db.close()

    if row is None:
        raise HTTPException(status_code=404, detail="No signing key found for this guild")
    return {"key_id": row.key_id, "private_key_pem": row.private_key_pem}


# ---------------------------------------------------------------------------
# Phase 1 — State writes
# ---------------------------------------------------------------------------


class ForemanTurnCreate(BaseModel):
    """Body for POST /guilds/{guild_id}/foreman/history."""

    user_id: str
    role: str  # "user" | "assistant" | "system"
    content_json: str  # JSON-serialised content blocks (string, list, …)
    is_tool_response: bool = False
    parent_id: int | None = None


@router.post("/guilds/{guild_id}/foreman/history")
async def create_foreman_turn(
    guild_id: str,
    body: ForemanTurnCreate,
    _caller: str = Depends(require_worker_or_member_path),
):
    """Persist one foreman conversation turn to the DB.

    Replaces ``_save_turn()`` in ``foreman/runner.py`` for the standalone
    foreman — it calls this endpoint instead of writing the DB directly.

    Response: ``{ "id": int, "created_at": str }``
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        created_at = datetime.now(UTC).isoformat()
        turn = ForemanTurn(
            guild_pk=guild_pk,
            user_id=body.user_id,
            role=body.role,
            content_json=body.content_json,
            is_tool_response=1 if body.is_tool_response else 0,
            parent_id=body.parent_id,
            created_at=created_at,
        )
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        return {"id": turn.id, "created_at": created_at}
    finally:
        await db.close()


class ForemanTaskCreate(BaseModel):
    """Body for POST /guilds/{guild_id}/tasks."""

    name: str
    description: str
    phase: str = "execute"
    user_id: str | None = None


@router.post("/guilds/{guild_id}/tasks")
async def create_foreman_task(
    guild_id: str,
    body: ForemanTaskCreate,
    _caller: str = Depends(require_worker_or_member_path),
):
    """Create a foreman-owned task (worker_id='foreman').

    Used by the standalone foreman's ``create_task`` tool in place of the
    direct DB insert currently in ``foreman/tools.py``.

    Response: ``{ "task_id": str, "created_at": str }``

    Also broadcasts a ``task-created`` WS event so the UI sidebar updates.
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")

        task_id = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        name = (body.name or "")[:80]
        created_at = datetime.now(UTC).isoformat()
        db.add(
            Task(
                id=task_id,
                worker_id=None,
                guild_pk=guild_pk,
                name=name,
                description=body.description,
                tool="claude",
                state="pending",
                phase=body.phase,
                created_at=created_at,
                user_id=body.user_id,
            )
        )
        await db.commit()
    finally:
        await db.close()

    await broadcast(
        guild_id,
        {
            "type": "task-created",
            "taskId": task_id,
            "name": name,
            "description": body.description,
            "phase": body.phase,
            "state": "pending",
            "createdAt": created_at,
        },
    )
    return {"task_id": task_id, "created_at": created_at}


class TaskPatch(BaseModel):
    """Body for PATCH /guilds/{guild_id}/tasks/{task_id}.

    All fields are optional — only non-None values are written to the DB.
    """

    state: str | None = None
    worker_id: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    finished_at: str | None = None
    deleted_at: str | None = None
    phase: str | None = None


@router.patch("/guilds/{guild_id}/tasks/{task_id}")
async def patch_task(
    guild_id: str,
    task_id: str,
    body: TaskPatch,
    _caller: str = Depends(require_worker_or_member_path),
):
    """Partially update a task's mutable fields.

    Used by the standalone foreman's tools (``assign_task``, ``finalize_task``,
    ``cancel_task``, ``send_followup``, ``redirect_task``) to write state
    changes without direct DB access.

    Accepted body fields:

    - ``state``: new task state
    - ``worker_id``: reassign to a different worker
    - ``branch``: git branch name
    - ``pr_url``: GitHub PR URL
    - ``finished_at``: ISO-8601 completion timestamp
    - ``deleted_at``: ISO-8601 soft-delete timestamp
    - ``phase``: task phase (plan / execute / review / followup)

    Broadcasts a ``task-update`` WS event with the changed fields so the
    frontend sidebar stays in sync.

    Response: ``{ "task_id": str, "updated": dict }``
    """
    update_values: dict = {}
    for field, col in (
        ("state", "state"),
        ("worker_id", "worker_id"),
        ("branch", "branch"),
        ("pr_url", "pr_url"),
        ("finished_at", "finished_at"),
        ("deleted_at", "deleted_at"),
        ("phase", "phase"),
    ):
        val = getattr(body, field)
        if val is not None:
            update_values[col] = val

    if not update_values:
        raise HTTPException(status_code=400, detail="No fields to update")

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")

        # Verify task exists in this guild
        exists = await db.execute(
            select(Task.id).where(Task.id == task_id, Task.guild_pk == guild_pk)
        )
        if exists.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Task not found")

        await db.execute(update(Task).where(Task.id == task_id).values(**update_values))
        await db.commit()
    finally:
        await db.close()

    # Broadcast task-update with changed fields (camelCase keys for WS protocol)
    _KEY_MAP = {
        "state": "state",
        "worker_id": "workerId",
        "branch": "branch",
        "pr_url": "prUrl",
        "finished_at": "finishedAt",
        "deleted_at": "deletedAt",
        "phase": "phase",
    }
    ws_payload: dict = {"type": "task-update", "taskId": task_id}
    for col, ws_key in _KEY_MAP.items():
        if col in update_values:
            ws_payload[ws_key] = update_values[col]
    await broadcast(guild_id, ws_payload)

    return {"task_id": task_id, "updated": update_values}


class MessageCreate(BaseModel):
    """Body for POST /guilds/{guild_id}/messages."""

    from_agent: str
    to_agent: str
    content: str
    message_type: str = "chat"
    user_id: str | None = None


@router.post("/guilds/{guild_id}/messages")
async def create_message(
    guild_id: str,
    body: MessageCreate,
    _caller: str = Depends(require_worker_or_member_path),
):
    """Persist a chat message sent by the external foreman.

    Used by the standalone foreman at the end of a ``run_foreman_ai()`` run
    to store the final text response in the ``messages`` table (the same
    write that the embedded foreman does in ``runner.py``).

    Also broadcasts a ``chat`` WS event so the frontend chat panel updates.

    Response: ``{ "message_id": int, "created_at": str }``
    """
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")

        created_at = datetime.now(UTC).isoformat()
        msg = Message(
            guild_pk=guild_pk,
            from_agent=body.from_agent,
            to_agent=body.to_agent,
            content=body.content,
            message_type=body.message_type,
            created_at=created_at,
            user_id=body.user_id,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        msg_id = msg.id
    finally:
        await db.close()

    await broadcast(
        guild_id,
        {
            "type": "chat",
            "from": body.from_agent,
            "to": body.to_agent,
            "content": body.content,
            "createdAt": created_at,
            **({"userId": body.user_id} if body.user_id else {}),
        },
    )
    return {"message_id": msg_id, "created_at": created_at}
