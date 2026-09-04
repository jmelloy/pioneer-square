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
"""

from __future__ import annotations

import logging

import discord_notifier
from auth_deps import require_member, require_worker_or_member_path
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from foreman.conversation_service import resolve_conversation_id
from foreman.runner import clear_foreman_history, get_foreman_history
from models import (
    Guild,
    Worker,
)
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn

router = APIRouter()
logger = logging.getLogger(__name__)


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
    result = await db.exec(select(col(Guild.id)).where(col(Guild.slug) == guild_id))
    guild_pk = result.one_or_none()
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    conversation_id = await resolve_conversation_id(db, guild_pk, user_id=github_user_id)
    history = await get_foreman_history(guild_id, github_user_id, conversation_id)
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
