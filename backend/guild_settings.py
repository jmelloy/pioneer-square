"""Helpers for reading guild-level settings outside the settings routes."""

from __future__ import annotations

from database import get_db
from models import Guild
from sqlmodel import col, select


async def get_guild_pre_agents_prompt(guild_slug: str) -> str | None:
    """Return the guild's pre-agents prompt (None if unset or on lookup failure).

    Opens its own short-lived session so callers that already hold a session
    (or none) can use it uniformly. Failures are swallowed: the prompt is an
    optional enhancement and must never block task assignment.
    """
    try:
        db = await get_db()
        try:
            res = await db.exec(
                select(Guild.pre_agents_prompt).where(col(Guild.slug) == guild_slug)
            )
            return res.first() or None
        finally:
            await db.close()
    except Exception:
        return None
