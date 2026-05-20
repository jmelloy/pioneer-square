"""Guild + guild-membership routes.

Owns ``/guilds`` (create/list) and ``/guilds/{id}`` (read/update), plus the
``/api/guilds/{id}/members`` CRUD surface.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member, require_user, require_worker_or_member_path
from database import get_db
from events import broadcast
from fastapi import APIRouter, Depends, HTTPException
from models import Agent, Guild, GuildMember, Message, User, Worker
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from utils import generate_guild_id, row_to_dict
from ws_handlers import _resolve_user_identifier

router = APIRouter()

_VALID_ROLES = {"owner", "member", "viewer"}


def _message_dict(m: Message) -> dict:
    """Serialize a Message row, merging any JSON stored in the `meta` column."""
    d = row_to_dict(m)
    if m.meta:
        try:
            d.update(json.loads(m.meta))
        except Exception:
            pass
    return d


class GuildCreate(BaseModel):
    name: str | None = None


class GuildUpdate(BaseModel):
    name: str | None = None
    primary_repo: str | None = None
    # A2A AgentCard fields
    description: str | None = None
    url: str | None = None
    version: str | None = None


class MemberCreate(BaseModel):
    # Either a users.id (numeric GitHub id as text) or a github_login.
    user: str
    role: str = "member"  # owner | member | viewer


class MemberUpdate(BaseModel):
    role: str  # owner | member | viewer


@router.post("/guilds")
async def create_guild(
    data: GuildCreate | None = None,
    github_user_id: str = Depends(require_user),
):
    if data is None:
        data = GuildCreate()
    created_at = datetime.now(UTC).isoformat()
    db = await get_db()
    try:
        result = await db.execute(text("SELECT guild_id FROM guilds WHERE deleted_at IS NULL"))
        existing_ids = {row[0] for row in result.fetchall()}
        guild_id = generate_guild_id(name=data.name or "", existing_ids=existing_ids)
        guild_name = data.name or f"Guild {guild_id}"
        try:
            new_guild = Guild(
                guild_id=guild_id,
                created_at=created_at,
                name=guild_name,
                github_user_id=github_user_id,
            )
            db.add(new_guild)
            await db.flush()
            db.add(
                GuildMember(
                    guild_pk=new_guild.id,
                    user_id=github_user_id,
                    role="owner",
                    created_at=created_at,
                )
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Could not generate unique guild ID")
    finally:
        await db.close()
    return {"id": guild_id, "created_at": created_at, "name": guild_name}


@router.get("/guilds")
async def list_guilds(github_user_id: str = Depends(require_user)):
    db = await get_db()
    try:
        result = await db.execute(
            select(
                Guild.guild_id.label("id"),
                Guild.created_at,
                Guild.name,
                func.count(Agent.id).label("agent_count"),
            )
            .select_from(Guild)
            .join(
                GuildMember,
                (GuildMember.guild_pk == Guild.id) & (GuildMember.user_id == github_user_id),
            )
            .outerjoin(
                Agent,
                (Agent.guild_pk == Guild.id)
                & (Agent.type != "foreman")
                & (Agent.state != "offline"),
            )
            .where(GuildMember.user_id == github_user_id)
            .group_by(Guild.guild_id)
            .order_by(Guild.created_at.desc())
        )
        return [dict(r._mapping) for r in result.fetchall()]
    finally:
        await db.close()


@router.patch("/guilds/{guild_id}")
async def update_guild(
    guild_id: str,
    data: GuildUpdate,
    github_user_id: str = Depends(require_member("owner")),
):
    db = await get_db()
    try:
        result = await db.execute(select(Guild).where(Guild.guild_id == guild_id))
        guild = result.scalar_one_or_none()
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")
        if data.name is not None:
            guild.name = data.name
        if "primary_repo" in data.model_fields_set:
            guild.primary_repo = data.primary_repo
        if "description" in data.model_fields_set:
            guild.description = data.description
        if "url" in data.model_fields_set:
            guild.url = data.url
        if "version" in data.model_fields_set:
            guild.version = data.version
        await db.commit()
    finally:
        await db.close()
    await broadcast(
        guild_id,
        {
            "type": "guild-updated",
            "id": guild_id,
            "name": guild.name,
            "primary_repo": guild.primary_repo,
            "description": guild.description,
            "url": guild.url,
            "version": guild.version,
        },
    )
    return {
        "id": guild_id,
        "name": guild.name,
        "primary_repo": guild.primary_repo,
        "description": guild.description,
        "url": guild.url,
        "version": guild.version,
    }


@router.get("/guilds/{guild_id}")
async def get_guild(guild_id: str, github_user_id: str = Depends(require_member())):
    db = await get_db()
    try:
        result = await db.execute(select(Guild).where(Guild.guild_id == guild_id))
        guild = result.scalar_one_or_none()
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")
        guild_pk = guild.id
        result = await db.execute(
            select(Agent, Worker.name.label("worker_name"))
            .outerjoin(Worker, Worker.id == Agent.worker_id)
            .where(
                Agent.guild_pk == guild_pk,
                Agent.state != "offline",
                Agent.type != "foreman",
            )
        )
        agent_rows = result.all()
        result = await db.execute(
            select(Message)
            .where(Message.guild_pk == guild_pk)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(100)
        )
        messages = result.scalars().all()
        return {
            **row_to_dict(guild),
            "id": guild.guild_id,  # keep text guild_id as "id" for API compatibility
            "agents": [
                {**row_to_dict(row.Agent), "worker_name": row.worker_name} for row in agent_rows
            ],
            "messages": [_message_dict(m) for m in reversed(messages)],
        }
    finally:
        await db.close()


@router.get("/api/guilds/{guild_id}/members")
async def list_guild_members(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
):
    """List members of a guild (caller must be a member)."""
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(
                GuildMember.user_id,
                GuildMember.role,
                GuildMember.created_at,
                User.github_login,
                User.display_name,
                User.avatar_url,
            )
            .outerjoin(User, User.id == GuildMember.user_id)
            .where(GuildMember.guild_pk == guild_pk)
            .order_by(GuildMember.created_at.asc())
        )
        return [dict(r._mapping) for r in result.fetchall()]
    finally:
        await db.close()


@router.post("/api/guilds/{guild_id}/members")
async def add_guild_member(
    guild_id: str,
    data: MemberCreate,
    github_user_id: str = Depends(require_member("owner")),
):
    """Add a member to a guild by users.id or github_login (owner only)."""
    if data.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role; must be one of {sorted(_VALID_ROLES)}"
        )
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        target_id = await _resolve_user_identifier(db, data.user)
        if not target_id:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"User '{data.user}' not found. They must log in to Pioneer Square once "
                    "before they can be added."
                ),
            )
        now = datetime.now(UTC).isoformat()
        stmt = sqlite_insert(GuildMember).values(
            guild_pk=guild_pk,
            user_id=target_id,
            role=data.role,
            created_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["guild_pk", "user_id"],
            set_={"role": stmt.excluded.role},
        )
        await db.execute(stmt)
        await db.commit()
        return {"guild_id": guild_id, "user_id": target_id, "role": data.role}
    finally:
        await db.close()


@router.patch("/api/guilds/{guild_id}/members/{user_id}")
async def update_guild_member(
    guild_id: str,
    user_id: str,
    data: MemberUpdate,
    github_user_id: str = Depends(require_member("owner")),
):
    """Change a member's role (owner only)."""
    if data.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role; must be one of {sorted(_VALID_ROLES)}"
        )
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        res = await db.execute(
            select(GuildMember).where(
                GuildMember.guild_pk == guild_pk, GuildMember.user_id == user_id
            )
        )
        member = res.scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        # Don't let the last owner demote themselves and lock the guild.
        if member.role == "owner" and data.role != "owner":
            owner_count = await db.execute(
                select(func.count())
                .select_from(GuildMember)
                .where(GuildMember.guild_pk == guild_pk, GuildMember.role == "owner")
            )
            if (owner_count.scalar() or 0) <= 1:
                raise HTTPException(
                    status_code=400, detail="Cannot demote the last owner of a guild"
                )
        member.role = data.role
        await db.commit()
        return {"guild_id": guild_id, "user_id": user_id, "role": data.role}
    finally:
        await db.close()


async def _ensure_webhook_secret(db, guild_id: str) -> str:
    """Return the guild's webhook secret, generating one on first access."""
    res = await db.execute(select(Guild).where(Guild.guild_id == guild_id))
    guild = res.scalar_one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    if not guild.webhook_secret:
        guild.webhook_secret = secrets.token_hex(32)
        await db.commit()
    return guild.webhook_secret


@router.post("/guilds/{guild_id}/webhook-secret")
async def rotate_webhook_secret(
    guild_id: str,
    github_user_id: str = Depends(require_member("owner")),
):
    """Generate a fresh webhook secret for the guild (owner only).

    Rotating invalidates webhooks already configured against the previous
    secret — callers must update each repo's webhook config to match.
    """
    db = await get_db()
    try:
        res = await db.execute(select(Guild).where(Guild.guild_id == guild_id))
        guild = res.scalar_one_or_none()
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")
        guild.webhook_secret = secrets.token_hex(32)
        await db.commit()
        return {"guild_id": guild_id, "webhook_secret": guild.webhook_secret}
    finally:
        await db.close()


@router.get("/guilds/{guild_id}/webhook-secret")
async def get_webhook_secret(
    guild_id: str,
    principal: str = Depends(require_worker_or_member_path),
):
    """Return the guild's webhook secret, generating one on first access.

    Accessible by guild members (any role) or registered workers — workers
    need it to configure ``POST /repos/{repo}/hooks`` on the user's behalf.
    """
    db = await get_db()
    try:
        secret = await _ensure_webhook_secret(db, guild_id)
        return {"guild_id": guild_id, "webhook_secret": secret}
    finally:
        await db.close()


@router.delete("/api/guilds/{guild_id}/members/{user_id}")
async def remove_guild_member(
    guild_id: str,
    user_id: str,
    github_user_id: str = Depends(require_member("owner")),
):
    """Remove a member from a guild (owner only)."""
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        res = await db.execute(
            select(GuildMember).where(
                GuildMember.guild_pk == guild_pk, GuildMember.user_id == user_id
            )
        )
        member = res.scalar_one_or_none()
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        if member.role == "owner":
            owner_count = await db.execute(
                select(func.count())
                .select_from(GuildMember)
                .where(GuildMember.guild_pk == guild_pk, GuildMember.role == "owner")
            )
            if (owner_count.scalar() or 0) <= 1:
                raise HTTPException(
                    status_code=400, detail="Cannot remove the last owner of a guild"
                )
        await db.execute(
            delete(GuildMember).where(
                GuildMember.guild_pk == guild_pk, GuildMember.user_id == user_id
            )
        )
        await db.commit()
        return {"status": "removed", "user_id": user_id}
    finally:
        await db.close()
