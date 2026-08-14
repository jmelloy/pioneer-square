"""Thread lifecycle routes: create, archive, close.

Part of the thread-per-conversation architecture (epic #1160, issue #1163).
A :class:`models.Thread` binds one :class:`models.Conversation` to a Discord
thread with an explicit lifecycle (``active`` -> ``archived``/``closed``).
Routes are guild-scoped (``/api/guilds/{guild_id}/threads``), matching every
other guild-owned resource in this app (see ``routes/tasks.py``,
``routes/guilds.py``) so ``require_member`` can enforce membership the same
way. The periodic sweep in ``foreman/thread_maintenance.py`` advances
``status`` automatically over time; these routes are the human/bot-triggered
counterpart.
"""

from __future__ import annotations

import random
import string
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from models import THREAD_STATUSES, Conversation, DiscordChannelGuild, Thread
from pydantic import BaseModel
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from ws_types import ThreadUpdateMsg

router = APIRouter()


def _new_thread_id() -> str:
    return "th-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


async def resolve_discord_guild_id(db: AsyncSession, ps_guild_slug: str) -> str | None:
    """Return the Discord server (guild) snowflake bound to *ps_guild_slug*, if any.

    Mirrors ``discord_notifier._resolve_channel_for_guild``'s lookup but returns the
    server id rather than a channel id — needed to build a
    ``https://discord.com/channels/{guild}/{thread}`` deep link. Multiple channels
    (and thus potentially multiple Discord servers) can be bound to one PS guild;
    the first-registered one wins, same tie-break as that lookup.
    """
    rows = (
        await db.exec(
            select(DiscordChannelGuild.discord_guild_id)
            .where(col(DiscordChannelGuild.ps_guild_id) == ps_guild_slug)
            .order_by(col(DiscordChannelGuild.created_at).asc())
        )
    ).all()
    return rows[0] if rows else None


def discord_thread_url(discord_guild_id: str | None, discord_thread_id: str | None) -> str | None:
    """Build a Discord deep link to a thread, or None if either id is missing."""
    if not discord_guild_id or not discord_thread_id:
        return None
    return f"https://discord.com/channels/{discord_guild_id}/{discord_thread_id}"


class ThreadCreate(BaseModel):
    # Bind to an existing conversation, or omit to start a new one for
    # (guild, user_id) — mirrors how a Discord thread is usually spun up the
    # first time a user starts talking to the Foreman.
    conversation_id: int | None = None
    user_id: str | None = None
    discord_thread_id: str | None = None
    name: str | None = None


class ThreadOut(BaseModel):
    id: str
    conversation_id: int
    discord_thread_id: str | None
    name: str | None
    status: str
    discord_url: str | None
    created_at: datetime
    updated_at: datetime


async def _to_out(db: AsyncSession, guild_id: str, thread: Thread) -> ThreadOut:
    discord_guild_id = await resolve_discord_guild_id(db, guild_id)
    return ThreadOut(
        id=thread.id,
        conversation_id=thread.conversation_id,
        discord_thread_id=thread.discord_thread_id,
        name=thread.name,
        status=thread.status,
        discord_url=discord_thread_url(discord_guild_id, thread.discord_thread_id),
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


@router.post("/api/guilds/{guild_id}/threads", response_model=ThreadOut)
async def create_thread(
    guild_id: str,
    body: ThreadCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Create a new thread, creating its conversation if needed.

    When ``conversation_id`` is omitted, reuses the caller's existing
    (guild, user) conversation if one exists, otherwise creates one — same
    "one conversation per (guild, user)" shape ``ForemanTurn`` history already
    assumes (see ``Conversation`` docstring in ``models.py``).
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    now = datetime.now(UTC)
    conversation_id = body.conversation_id
    if conversation_id is not None:
        result = await db.exec(
            select(col(Conversation.id)).where(
                col(Conversation.id) == conversation_id, col(Conversation.guild_id) == guild_pk
            )
        )
        if result.one_or_none() is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        user_id = body.user_id or github_user_id
        result = await db.exec(
            select(Conversation).where(
                col(Conversation.guild_id) == guild_pk, col(Conversation.user_id) == user_id
            )
        )
        conversation = result.first()
        if conversation is None:
            conversation = Conversation(
                guild_id=guild_pk, user_id=user_id, created_at=now, updated_at=now
            )
            db.add(conversation)
            await db.flush()
        conversation_id = conversation.id

    thread = Thread(
        id=_new_thread_id(),
        conversation_id=conversation_id,
        discord_thread_id=body.discord_thread_id,
        name=body.name,
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    return await _to_out(db, guild_id, thread)


async def _get_thread_in_guild(db: AsyncSession, guild_id: str, thread_id: str) -> Thread:
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(Thread)
        .join(Conversation, col(Thread.conversation_id) == col(Conversation.id))
        .where(
            col(Thread.id) == thread_id,
            col(Conversation.guild_id) == guild_pk,
            col(Thread.deleted_at).is_(None),
        )
    )
    thread = result.first()
    if thread is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


async def _set_status(
    guild_id: str,
    thread_id: str,
    new_status: str,
    github_user_id: str,
    db: AsyncSession,
    *,
    soft_delete: bool = False,
) -> ThreadOut:
    assert new_status in THREAD_STATUSES
    thread = await _get_thread_in_guild(db, guild_id, thread_id)
    now = datetime.now(UTC)
    thread.status = new_status
    thread.updated_at = now
    if soft_delete:
        thread.deleted_at = now
    db.add(thread)
    await db.commit()
    await db.refresh(thread)
    out = await _to_out(db, guild_id, thread)
    await broadcast_msg(
        guild_id,
        ThreadUpdateMsg(
            threadId=out.id,
            status=out.status,
            name=out.name,
            discordThreadId=out.discord_thread_id,
            discordUrl=out.discord_url,
            deletedAt=thread.deleted_at.isoformat() if thread.deleted_at else None,
        ),
    )
    return out


@router.patch("/api/guilds/{guild_id}/threads/{thread_id}/archive", response_model=ThreadOut)
async def archive_thread(
    guild_id: str,
    thread_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Mark a thread archived (read-only in Discord, still resolvable by the backend)."""
    return await _set_status(guild_id, thread_id, "archived", github_user_id, db)


@router.patch("/api/guilds/{guild_id}/threads/{thread_id}/close", response_model=ThreadOut)
async def close_thread(
    guild_id: str,
    thread_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Mark a thread closed (done, eligible for eventual soft-delete cleanup)."""
    return await _set_status(guild_id, thread_id, "closed", github_user_id, db)


@router.delete("/api/guilds/{guild_id}/threads/{thread_id}", response_model=ThreadOut)
async def delete_thread(
    guild_id: str,
    thread_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Soft-delete a thread (``deleted_at`` stamp; excluded from get/list from here on).

    Distinct from ``close``: a closed thread still shows up as history, a deleted
    one doesn't. Marks ``status="closed"`` too so any stale in-flight reference
    (e.g. a cached frontend row) reads the terminal state consistently.
    """
    return await _set_status(guild_id, thread_id, "closed", github_user_id, db, soft_delete=True)


@router.get("/api/guilds/{guild_id}/threads/{thread_id}", response_model=ThreadOut)
async def get_thread(
    guild_id: str,
    thread_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    return await _to_out(db, guild_id, await _get_thread_in_guild(db, guild_id, thread_id))


@router.get("/api/guilds/{guild_id}/threads", response_model=list[ThreadOut])
async def list_threads(
    guild_id: str,
    status: str | None = None,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    stmt = (
        select(Thread)
        .join(Conversation, col(Thread.conversation_id) == col(Conversation.id))
        .where(col(Conversation.guild_id) == guild_pk, col(Thread.deleted_at).is_(None))
        .order_by(col(Thread.created_at).desc())
    )
    if status is not None:
        if status not in THREAD_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status!r}")
        stmt = stmt.where(col(Thread.status) == status)
    result = await db.exec(stmt.limit(200))
    return [await _to_out(db, guild_id, t) for t in result.all()]
