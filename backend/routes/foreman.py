"""Foreman conversation-history routes (debug + clear).

The foreman AI itself runs in ``foreman/runner.py``; this module is just the
HTTP surface for inspecting and clearing per-guild/per-user turn history.
"""

from __future__ import annotations

import logging

from auth_deps import require_member
from database import get_db
from fastapi import APIRouter, Depends, HTTPException
from foreman import clear_foreman_history, get_foreman_history
from models import Guild
from sqlalchemy import select

router = APIRouter()
logger = logging.getLogger(__name__)


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
