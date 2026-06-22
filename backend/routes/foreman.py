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
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException, Query
from foreman.runner import _fetch_online_workers
from models import (
    ForemanTurn,
    Guild,
    GuildKey,
    GuildMember,
    Message,
    Task,
    live_tasks_filter,
)
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import ChatMsg, TaskCreatedMsg, TaskUpdateMsg

from foreman import clear_foreman_history, get_foreman_history

router = APIRouter()
logger = logging.getLogger(__name__)


async def _require_task_in_guild(db: AsyncSession, task_id: str, guild_pk: int) -> None:
    """Raise HTTP 404 if task_id does not exist or belongs to a different guild.

    Task.id is the string PK (e.g. "t-abc123"), not an integer. guild_pk is the
    integer FK from guilds.id. This check enforces guild ownership, which a bare
    FK constraint cannot express.
    """
    result = await db.exec(
        select(col(Task.id)).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
    )
    if result.one_or_none() is None:
        raise HTTPException(
            status_code=404, detail="Task not found or does not belong to this guild"
        )


# ---------------------------------------------------------------------------
# Existing debug endpoints (unchanged)
# ---------------------------------------------------------------------------


@router.get("/guilds/{guild_id}/foreman/context")
async def get_foreman_context(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the stored foreman conversation turns for this guild+user (debug view)."""
    result = await db.exec(select(col(Guild.slug)).where(col(Guild.slug) == guild_id))
    if result.one_or_none() is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    history = await get_foreman_history(guild_id, github_user_id)
    return {
        "system": history["system"],
        "messages": history["messages"],
        "count": len(history["messages"]),
        "total": history["total"],
    }


@router.post("/guilds/{guild_id}/foreman/clear-context")
async def clear_foreman_context(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Delete all stored foreman turns for this guild+user. Chat history in messages table is preserved."""
    result = await db.exec(select(col(Guild.slug)).where(col(Guild.slug) == guild_id))
    if result.one_or_none() is None:
        raise HTTPException(status_code=404, detail="Guild not found")
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
    db: AsyncSession = Depends(get_db_dep),
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
              "pr_url": str | null, "deleted_at": str | null
            }
          ]
        }

    ``workers`` lists only online workers (state == 'online'). ``tasks``
    lists all non-terminal, non-soft-deleted tasks, most recent first.
    Mirrors the state snapshot built by ``run_foreman_ai()`` so the
    standalone foreman can construct the same system-prompt preamble.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    # Guild metadata
    guild_res = await db.exec(
        select(col(Guild.name), col(Guild.primary_repo)).where(col(Guild.slug) == guild_id)
    )
    guild_row = guild_res.one_or_none()

    # Fetch guild owner user_id for the standalone foreman
    owner_res = await db.exec(
        select(col(GuildMember.user_id))
        .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
        .limit(1)
    )
    owner_user_id = owner_res.one_or_none()

    guild_data = {
        "name": guild_row.name if guild_row else None,
        "primary_repo": guild_row.primary_repo if guild_row else None,
        "owner_user_id": owner_user_id,
    }

    # Online workers with their active agents
    worker_rows = await _fetch_online_workers(db, guild_id)
    workers_data = [
        {
            "id": r["id"],
            "state": r["worker_state"] or "idle",
            "repos": json.loads(r["repos"] or "[]"),
            **({"org": r["org"]} if r.get("org") else {}),
            "agent_count": r["agent_count"] or 0,
            "agents": r["agents"] or "",
            "tools": json.loads(r["tools"] or "[]"),
        }
        for r in worker_rows
    ]

    # Active (non-terminal, non-soft-deleted) tasks
    _TERMINAL = {"done", "failed", "cancelled"}
    tasks_res = await db.exec(
        select(
            col(Task.id),
            col(Task.worker_id),
            col(Task.name),
            col(Task.description),
            col(Task.state),
            col(Task.phase),
            col(Task.branch),
            col(Task.pr_url),
            col(Task.deleted_at),
            col(Task.created_at),
            col(Task.user_id),
        )
        .where(
            col(Task.guild_id) == guild_pk,
            ~col(Task.state).in_(list(_TERMINAL)),
            live_tasks_filter(),
        )
        .order_by(col(Task.created_at).desc())
    )
    tasks_data = [dict(r._mapping) for r in tasks_res.all()]

    return {"guild": guild_data, "workers": workers_data, "tasks": tasks_data}


@router.get("/guilds/{guild_id}/foreman/history")
async def get_foreman_history_for_user(
    guild_id: str,
    user_id: str = Query(..., description="GitHub user_id whose thread to fetch"),
    limit: int | None = Query(default=None, description="Max rows to return (newest first)"),
    _caller: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
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
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    stmt = (
        select(
            col(ForemanTurn.id),
            col(ForemanTurn.role),
            col(ForemanTurn.content_json),
            col(ForemanTurn.is_tool_response),
            col(ForemanTurn.parent_id),
            col(ForemanTurn.created_at),
            col(ForemanTurn.api_calls_json),
        )
        .where(col(ForemanTurn.guild_id) == guild_pk, col(ForemanTurn.user_id) == user_id)
        .order_by(col(ForemanTurn.id))
    )
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    result = await db.exec(stmt)
    turns = result.all()

    return [
        {
            "id": t.id,
            "role": t.role,
            "content_json": t.content_json,
            "is_tool_response": bool(t.is_tool_response),
            "parent_id": t.parent_id,
            "created_at": t.created_at,
            "api_calls_json": t.api_calls_json,
        }
        for t in turns
    ]


@router.get("/guilds/{guild_id}/foreman/env-vars")
async def get_foreman_env_vars(
    guild_id: str,
    _caller: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the guild's foreman env vars with real (unmasked) values.

    Used by standalone workers at startup to apply guild-configured API keys
    (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) into their process environment.
    Requires a valid worker auth_token or member login_token.

    Response: ``{ "env_vars": [{"key": str, "value": str}, ...] }``
    """
    res = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = res.one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    config = guild.foreman_config or {}
    return {"env_vars": config.get("env_vars", [])}


@router.get("/guilds/{guild_id}/guild-key")
async def get_guild_key(
    guild_id: str,
    _caller: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the guild's Ed25519 signing key for JWT operations.

    Response shape::

        { "key_id": str, "private_key_pem": str }

    Used by the standalone foreman's ``dnsid sign`` tool to create JWTs
    without direct DB access. Returns 404 if no key has been generated yet.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(GuildKey.key_id), col(GuildKey.private_key_pem)).where(
            col(GuildKey.guild_id) == guild_pk
        )
    )
    row = result.one_or_none()

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
    api_calls: list | None = None  # per-HTTP-call metadata from tool execution
    task_id: str | None = None


@router.post("/guilds/{guild_id}/foreman/history")
async def create_foreman_turn(
    guild_id: str,
    body: ForemanTurnCreate,
    _caller: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Persist one foreman conversation turn to the DB.

    Replaces ``_save_turn()`` in ``foreman/runner.py`` for the standalone
    foreman — it calls this endpoint instead of writing the DB directly.

    Response: ``{ "id": int, "created_at": str }``
    """
    import json as _json

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    if body.task_id is not None:
        await _require_task_in_guild(db, body.task_id, guild_pk)
    created_at = datetime.now(UTC)
    turn = ForemanTurn(
        guild_id=guild_pk,
        user_id=body.user_id,
        role=body.role,
        content_json=body.content_json,
        is_tool_response=1 if body.is_tool_response else 0,
        parent_id=body.parent_id,
        created_at=created_at,
        api_calls_json=_json.dumps(body.api_calls) if body.api_calls else None,
        task_id=body.task_id,
    )
    db.add(turn)
    await db.commit()
    await db.refresh(turn)
    return {"id": turn.id, "created_at": created_at}


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
    db: AsyncSession = Depends(get_db_dep),
):
    """Create a foreman-owned task (worker_id=None — unassigned until a worker picks it up).

    Used by the standalone foreman's ``create_task`` tool in place of the
    direct DB insert currently in ``foreman/tools.py``.

    Response: ``{ "task_id": str, "created_at": str }``

    Also broadcasts a ``task-created`` WS event so the UI sidebar updates.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    task_id = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    name = (body.name or "")[:80]
    created_at = datetime.now(UTC)
    db.add(
        Task(
            id=task_id,
            worker_id=None,
            guild_id=guild_pk,
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

    await broadcast_msg(
        guild_id,
        TaskCreatedMsg(
            taskId=task_id,
            name=name,
            description=body.description,
            phase=body.phase,
            state="pending",
            createdAt=created_at.isoformat(),
        ),
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
    deleted_at: str | None = None
    phase: str | None = None


@router.patch("/guilds/{guild_id}/tasks/{task_id}")
async def patch_task(
    guild_id: str,
    task_id: str,
    body: TaskPatch,
    _caller: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
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
    - ``deleted_at``: ISO-8601 soft-delete / expiry timestamp
    - ``phase``: task phase (plan / execute / review / followup)

    Broadcasts a ``task-update`` WS event with the changed fields so the
    frontend sidebar stays in sync.

    Response: ``{ "task_id": str, "updated": dict }``
    """
    update_values: dict = {}
    for field, col_name in (
        ("state", "state"),
        ("worker_id", "worker_id"),
        ("branch", "branch"),
        ("pr_url", "pr_url"),
        ("deleted_at", "deleted_at"),
        ("phase", "phase"),
    ):
        val = getattr(body, field)
        if val is not None:
            update_values[col_name] = val

    if not update_values:
        raise HTTPException(status_code=400, detail="No fields to update")

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    # Verify task exists in this guild
    exists = await db.exec(
        select(col(Task.id)).where(col(Task.id) == task_id, col(Task.guild_id) == guild_pk)
    )
    if exists.one_or_none() is None:
        raise HTTPException(status_code=404, detail="Task not found")

    await db.exec(update(Task).where(col(Task.id) == task_id).values(**update_values))
    await db.commit()

    # Broadcast task-update with changed fields (camelCase keys for WS protocol)
    _KEY_MAP = {
        "state": "state",
        "worker_id": "workerId",
        "branch": "branch",
        "pr_url": "prUrl",
        "deleted_at": "deletedAt",
        "phase": "phase",
    }
    ws_data: dict = {"taskId": task_id}
    for col_name, ws_key in _KEY_MAP.items():
        if col_name in update_values:
            ws_data[ws_key] = update_values[col_name]
    await broadcast_msg(guild_id, TaskUpdateMsg.model_validate(ws_data))

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
    db: AsyncSession = Depends(get_db_dep),
):
    """Persist a chat message sent by the external foreman.

    Used by the standalone foreman at the end of a ``run_foreman_ai()`` run
    to store the final text response in the ``messages`` table (the same
    write that the embedded foreman does in ``runner.py``).

    Also broadcasts a ``chat`` WS event so the frontend chat panel updates.

    Response: ``{ "message_id": int, "created_at": str }``
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    created_at = datetime.now(UTC)
    msg = Message(
        guild_id=guild_pk,
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

    await broadcast_msg(
        guild_id,
        ChatMsg(
            from_=body.from_agent,
            to=body.to_agent,
            content=body.content,
            createdAt=created_at.isoformat(),
            **({"userId": body.user_id} if body.user_id else {}),
        ),
    )
    return {"message_id": msg_id, "created_at": created_at}


# ---------------------------------------------------------------------------
# Phase 3 — Token counts + tool execution for standalone foreman
# ---------------------------------------------------------------------------


class TurnTokensUpdate(BaseModel):
    input_tokens: int
    output_tokens: int


@router.patch("/guilds/{guild_id}/foreman/turns/{turn_id}/tokens")
async def update_turn_tokens(
    guild_id: str,
    turn_id: int,
    body: TurnTokensUpdate,
    _caller: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Update token counts for a saved foreman turn. Used by the standalone foreman."""
    from sqlalchemy import update as sa_update

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    await db.exec(
        sa_update(ForemanTurn)
        .where(col(ForemanTurn.id) == turn_id)
        .values(input_tokens=body.input_tokens, output_tokens=body.output_tokens)
    )
    await db.commit()
    return {"ok": True}


class ToolExecRequest(BaseModel):
    tool_name: str
    tool_id: str
    tool_input: dict
    user_id: str | None = None


@router.post("/guilds/{guild_id}/foreman/exec_tool")
async def exec_tool(
    guild_id: str,
    body: ToolExecRequest,
    _caller: str = Depends(require_worker_or_member_path),
):
    """Execute a single foreman tool call. Used by the standalone foreman process.

    This delegates to _exec_one_tool() in foreman/tools.py, keeping all business
    logic (locks, worker selection, DB writes, WS broadcasts) in the backend.

    Returns a tool_result block: {"type": "tool_result", "tool_use_id": str, "content": str, "is_error": bool}
    """
    from foreman.tools import _exec_one_tool

    class _FakeToolUse:
        def __init__(self):
            self.name = body.tool_name
            self.id = body.tool_id
            self.input = body.tool_input

    return await _exec_one_tool(guild_id, _FakeToolUse(), body.user_id)
