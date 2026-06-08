"""Guild + guild-membership routes.

Owns ``/guilds`` (create/list) and ``/guilds/{id}`` (read/update), plus the
``/api/guilds/{id}/members`` CRUD surface.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member, require_user, require_worker_or_member_path
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from models import Agent, Guild, GuildMember, Message, User, Worker
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from utils import generate_guild_id, row_to_dict
from ws_handlers import _resolve_user_identifier
from ws_types import GuildUpdatedMsg

router = APIRouter()
logger = logging.getLogger(__name__)

_VALID_ROLES = {"owner", "member", "viewer"}


def _message_dict(m: Message) -> dict:
    """Serialize a Message row, merging any JSON stored in the `meta` column."""
    d = row_to_dict(m)
    if m.meta:
        try:
            d.update(json.loads(m.meta))
        except json.JSONDecodeError:
            logger.warning("guild messages: failed to parse meta JSON for message id=%s", m.id)
            d["metaParseError"] = True
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


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_FOREMAN_ENV_VARS = 20
_MAX_ENV_VALUE_LEN = 4096


class EnvVarItem(BaseModel):
    key: str
    # None → keep the currently stored value. Kept for API compatibility; the UI
    # now sends actual values since env vars are returned in clear text.
    value: str | None = None


class ForemanConfigUpdate(BaseModel):
    model: str | None = None
    provider: str | None = None
    system_prompt_suffix: str | None = Field(default=None, max_length=10000)
    max_rounds: int | None = Field(default=None, gt=0)
    poll_min_interval: int | None = Field(default=None, gt=0)
    poll_max_interval: int | None = Field(default=None, gt=0)
    # None (field absent) → leave existing env_vars unchanged.
    # Empty list → clear all env_vars.
    env_vars: list[EnvVarItem] | None = None

    @field_validator("env_vars")
    @classmethod
    def validate_env_vars(cls, v: list[EnvVarItem] | None) -> list[EnvVarItem] | None:
        if v is None:
            return v
        if len(v) > _MAX_FOREMAN_ENV_VARS:
            raise ValueError(f"Too many env vars (max {_MAX_FOREMAN_ENV_VARS})")
        for item in v:
            if not _ENV_KEY_RE.match(item.key):
                raise ValueError(
                    f"Invalid env var key {item.key!r}; must match ^[A-Za-z_][A-Za-z0-9_]*$"
                )
            if item.value is not None and len(item.value) > _MAX_ENV_VALUE_LEN:
                raise ValueError(
                    f"Value for {item.key!r} exceeds max length ({_MAX_ENV_VALUE_LEN} chars)"
                )
        return v


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
    db: AsyncSession = Depends(get_db_dep),
):
    if data is None:
        data = GuildCreate()
    created_at = datetime.now(UTC)
    result = await db.exec(select(col(Guild.slug)).where(col(Guild.deleted_at).is_(None)))
    existing_ids = set(result.all())
    guild_id = generate_guild_id(name=data.name or "", existing_ids=existing_ids)
    guild_name = data.name or f"Guild {guild_id}"
    try:
        new_guild = Guild(
            slug=guild_id,
            created_at=created_at,
            name=guild_name,
            github_user_id=github_user_id,
        )
        db.add(new_guild)
        await db.flush()
        db.add(
            GuildMember(
                guild_id=new_guild.id or 0,
                user_id=github_user_id,
                role="owner",
                created_at=created_at,
            )
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Could not generate unique guild ID")
    return {"id": guild_id, "created_at": created_at, "name": guild_name}


@router.get("/guilds")
async def list_guilds(
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    result = await db.exec(
        select(
            col(Guild.slug).label("id"),
            col(Guild.created_at),
            col(Guild.name),
            func.count(col(Agent.id)).label("agent_count"),
        )
        .select_from(Guild)
        .join(
            GuildMember,
            (col(GuildMember.guild_id) == col(Guild.id))
            & (col(GuildMember.user_id) == github_user_id),
        )
        .outerjoin(
            Agent,
            (col(Agent.guild_id) == col(Guild.id))
            & (col(Agent.type) != "foreman")
            & (col(Agent.state) != "offline"),
        )
        .where(col(GuildMember.user_id) == github_user_id)
        .group_by(col(Guild.slug), col(Guild.created_at), col(Guild.name))
        .order_by(col(Guild.created_at).desc())
    )
    return [dict(r._mapping) for r in result.all()]


@router.patch("/guilds/{guild_id}")
async def update_guild(
    guild_id: str,
    data: GuildUpdate,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    result = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = result.one_or_none()
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
    await broadcast_msg(
        guild_id,
        GuildUpdatedMsg(
            id=guild_id,
            name=guild.name,
            primary_repo=guild.primary_repo,
            description=guild.description,
            url=guild.url,
            version=guild.version,
        ),
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
async def get_guild(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    result = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = result.one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    guild_pk = guild.id
    result = await db.exec(
        select(Agent, col(Worker.name).label("worker_name"))
        .outerjoin(Worker, col(Worker.id) == col(Agent.worker_id))
        .where(
            col(Agent.guild_id) == guild_pk,
            col(Agent.state) != "offline",
            col(Agent.type) != "foreman",
        )
    )
    agent_rows = result.all()
    result = await db.exec(
        select(Message)
        .where(col(Message.guild_id) == guild_pk)
        # .id.desc() is a stable tiebreaker because message IDs are auto-increment integers.
        .order_by(col(Message.created_at).desc(), col(Message.id).desc())
        .limit(100)
    )
    messages = result.all()
    return {
        **row_to_dict(guild),
        "id": guild.slug,  # keep slug as "id" for API compatibility
        "agents": [
            {**row_to_dict(row.Agent), "worker_name": row.worker_name} for row in agent_rows
        ],
        "messages": [_message_dict(m) for m in reversed(messages)],
    }


@router.get("/api/guilds/{guild_id}/members")
async def list_guild_members(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List members of a guild (caller must be a member)."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(
            col(GuildMember.user_id),
            col(GuildMember.role),
            col(GuildMember.created_at),
            col(User.github_login),
            col(User.display_name),
            col(User.avatar_url),
        )
        .outerjoin(User, col(User.id) == col(GuildMember.user_id))
        .where(col(GuildMember.guild_id) == guild_pk)
        .order_by(col(GuildMember.created_at).asc())
    )
    return [dict(r._mapping) for r in result.all()]


@router.post("/api/guilds/{guild_id}/members")
async def add_guild_member(
    guild_id: str,
    data: MemberCreate,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Add a member to a guild by users.id or github_login (owner only)."""
    if data.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role; must be one of {sorted(_VALID_ROLES)}"
        )
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
    now = datetime.now(UTC)
    stmt = pg_insert(GuildMember).values(
        guild_id=guild_pk,
        user_id=target_id,
        role=data.role,
        created_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["guild_id", "user_id"],
        set_={"role": stmt.excluded.role},
    )
    await db.exec(stmt)
    await db.commit()
    return {"slug": guild_id, "user_id": target_id, "role": data.role}


@router.patch("/api/guilds/{guild_id}/members/{user_id}")
async def update_guild_member(
    guild_id: str,
    user_id: str,
    data: MemberUpdate,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Change a member's role (owner only)."""
    if data.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role; must be one of {sorted(_VALID_ROLES)}"
        )
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    res = await db.exec(
        select(GuildMember).where(
            col(GuildMember.guild_id) == guild_pk, col(GuildMember.user_id) == user_id
        )
    )
    member = res.one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    # Don't let the last owner demote themselves and lock the guild.
    if member.role == "owner" and data.role != "owner":
        owner_count = await db.exec(
            select(func.count())
            .select_from(GuildMember)
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
        )
        if (owner_count.one() or 0) <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last owner of a guild")
    member.role = data.role
    await db.commit()
    return {"slug": guild_id, "user_id": user_id, "role": data.role}


async def _ensure_webhook_secret(db, guild_id: str) -> str:
    """Return the guild's webhook secret, generating one on first access."""
    res = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = res.one_or_none()
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
    db: AsyncSession = Depends(get_db_dep),
):
    """Generate a fresh webhook secret for the guild (owner only).

    Rotating invalidates webhooks already configured against the previous
    secret — callers must update each repo's webhook config to match.
    """
    res = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = res.one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    guild.webhook_secret = secrets.token_hex(32)
    await db.commit()
    return {"slug": guild_id, "webhook_secret": guild.webhook_secret}


@router.get("/guilds/{guild_id}/webhook-secret")
async def get_webhook_secret(
    guild_id: str,
    principal: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the guild's webhook secret, generating one on first access.

    Accessible by guild members (any role) or registered workers — workers
    need it to configure ``POST /repos/{repo}/hooks`` on the user's behalf.
    """
    secret = await _ensure_webhook_secret(db, guild_id)
    return {"slug": guild_id, "webhook_secret": secret}


@router.get("/api/guilds/{guild_id}/foreman-config")
async def get_foreman_config(
    guild_id: str,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the guild's foreman configuration (owner only).

    Env var values are returned in clear text (owner-only) so the settings
    dialogue can display and copy/paste them for verification.
    """
    res = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = res.one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    return guild.foreman_config or {}


@router.patch("/api/guilds/{guild_id}/foreman-config")
async def update_foreman_config(
    guild_id: str,
    data: ForemanConfigUpdate,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Update (merge) the guild's foreman configuration (owner only).

    Env var values sent as null preserve the existing stored value (kept for API
    compatibility); an empty string or a new string replaces the stored value.
    Keys absent from the submitted list are deleted.
    """
    res = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = res.one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    config: dict = dict(guild.foreman_config or {})
    for field in data.model_fields_set:
        value = getattr(data, field)
        if field == "env_vars":
            if value is None:
                # Explicit null → clear all env vars
                config.pop("env_vars", None)
            else:
                existing_map: dict[str, str] = {
                    e["key"]: e["value"] for e in config.get("env_vars", [])
                }
                new_env_vars: list[dict] = []
                for item in value:
                    if item.value is None:
                        # Unchanged: keep the existing stored value if present
                        if item.key in existing_map:
                            new_env_vars.append({"key": item.key, "value": existing_map[item.key]})
                    else:
                        new_env_vars.append({"key": item.key, "value": item.value})
                config["env_vars"] = new_env_vars
        elif value is None:
            config.pop(field, None)
        else:
            config[field] = value
    guild.foreman_config = config
    await db.commit()
    return config


@router.delete("/api/guilds/{guild_id}/members/{user_id}")
async def remove_guild_member(
    guild_id: str,
    user_id: str,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Remove a member from a guild (owner only)."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    res = await db.exec(
        select(GuildMember).where(
            col(GuildMember.guild_id) == guild_pk, col(GuildMember.user_id) == user_id
        )
    )
    member = res.one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.role == "owner":
        owner_count = await db.exec(
            select(func.count())
            .select_from(GuildMember)
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
        )
        if (owner_count.one() or 0) <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove the last owner of a guild")
    await db.exec(
        delete(GuildMember).where(
            col(GuildMember.guild_id) == guild_pk, col(GuildMember.user_id) == user_id
        )
    )
    await db.commit()
    return {"status": "removed", "user_id": user_id}
