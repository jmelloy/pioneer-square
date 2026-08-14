"""Foreman REST API — conversation history, debug context, and tool/state helpers.

Auth: helper endpoints accept either a worker auth_token *or* a member
login_token via ``require_worker_or_member_path``. The standalone Foreman API
proxy no longer uses these endpoints; the backend owns the Foreman loop and
only delegates LLM API calls over WebSocket.

Endpoint summary
----------------
Existing (unchanged):
  GET  /guilds/{guild_id}/foreman/context        — debug: stored turns for calling user
  POST /guilds/{guild_id}/foreman/clear-context  — debug: delete all turns for calling user

State reads:
  GET  /guilds/{guild_id}/foreman/env-vars       — guild foreman env vars (unmasked)

State writes:
  POST  /guilds/{guild_id}/tasks                 — create a foreman-owned task
  PATCH /guilds/{guild_id}/tasks/{task_id}       — update task fields (state, worker, branch, …)
  POST  /guilds/{guild_id}/messages              — persist a chat message
"""

from __future__ import annotations

import logging
import random
import string
from datetime import UTC, datetime
from typing import Literal

import discord_notifier
from auth_deps import get_guild_pk, require_member, require_worker_or_member_path
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from foreman.runner import clear_foreman_history, get_foreman_history
from models import (
    Guild,
    Message,
    Task,
    Worker,
)
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from ws_types import ChatMsg, TaskCreatedMsg, TaskUpdateMsg

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
    # Clearing history is the closest existing "conversation closed" signal —
    # archive this user's per-conversation Discord thread (#1161) to match.
    # Fire-and-forget: Discord API latency must not block the response.
    spawn(
        discord_notifier.archive_conversation_thread(guild_id, github_user_id),
        name=f"discord.archive-conversation:{guild_id}",
    )
    return {"status": "cleared", "removed": removed}


# ---------------------------------------------------------------------------
# State reads
# ---------------------------------------------------------------------------


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

    ``env_vars`` are shared across every tool; ``tool_env_vars`` are scoped to a
    single worker tool (claude/pi/codex) and must not leak into the others. Only
    shared vars marked ``forward=True`` are returned — unshared ones stay with
    the foreman's own LLM and never reach a worker.

    Response: ``{ "env_vars": [{"key", "value"}, ...],
                  "tool_env_vars": {"claude": [...], "pi": [...], "codex": [...]} }``
    """
    from spawn_config import resolve_spawn  # noqa: PLC0415

    res = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = res.one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    # Resolve the worker's owner so this user's spawn-settings override (env_vars
    # + per-tool tool_env_vars) layers over the guild baseline, matching what the
    # worker's container was launched with. A worker principal maps to its
    # Worker.user_id; a member principal is that member; otherwise baseline only.
    user_id: str | None = None
    excluded: set[str] = set()
    if _caller.startswith("worker:"):
        worker_id = _caller.split(":", 1)[1]
        row = (
            await db.exec(
                select(col(Worker.user_id), col(Worker.excluded_env_keys)).where(
                    col(Worker.id) == worker_id
                )
            )
        ).one_or_none()
        if row is not None:
            user_id, excluded_keys = row
            # Keys the operator opted this worker's launch out of. Without this
            # the worker would re-acquire here exactly what was withheld from
            # its container env.
            excluded = set(excluded_keys or [])
    elif _caller.startswith("user:"):
        user_id = _caller.split(":", 1)[1]
    resolved = await resolve_spawn(db, guild.id, user_id)
    return {
        "env_vars": [
            {"key": k, "value": v} for k, v in resolved.env_vars.items() if k not in excluded
        ],
        "tool_env_vars": {
            tool: [{"key": k, "value": v} for k, v in (kv or {}).items()]
            for tool, kv in resolved.tool_env_vars.items()
        },
    }


# ---------------------------------------------------------------------------
# Phase 1 — State writes
# ---------------------------------------------------------------------------


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
    - ``phase``: task phase (issue / plan / execute / review / followup)

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
    task_id: str | None = None
    source: Literal["web", "discord", "api"] | None = None


@router.post("/guilds/{guild_id}/messages")
async def create_message(
    guild_id: str,
    body: MessageCreate,
    _caller: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Persist a chat message from an authenticated worker/API caller.

    Also broadcasts a ``chat`` WS event so the frontend chat panel updates.

    Response: ``{ "message_id": int, "created_at": str }``
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    created_at = datetime.now(UTC)
    if body.task_id is not None:
        await _require_task_in_guild(db, body.task_id, guild_pk)
    source = body.source or "web"
    msg = Message(
        guild_id=guild_pk,
        from_agent=body.from_agent,
        to_agent=body.to_agent,
        content=body.content,
        message_type=body.message_type,
        created_at=created_at,
        user_id=body.user_id,
        task_id=body.task_id,
        source=source,
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
            source=source,
            taskId=body.task_id,
            **({"userId": body.user_id} if body.user_id else {}),
        ),
    )
    return {"message_id": msg_id, "created_at": created_at}
