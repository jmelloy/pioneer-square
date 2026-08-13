"""Guild + guild-membership routes.

Owns ``/guilds`` (create/list) and ``/guilds/{id}`` (read/update), plus the
``/api/guilds/{id}/members`` CRUD surface.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member, require_user, require_worker_or_member_path
from database import get_db_dep
from events import broadcast_msg
from fastapi import APIRouter, Depends, HTTPException
from models import Agent, Guild, GuildInvite, GuildMember, Message, User, Worker
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, text
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
    # Expose the per-task child-context id under the same camelCase key the live
    # WS broadcast (ChatMsg.taskId) and the frontend badge read. row_to_dict
    # yields the raw column name `task_id`, which the chat pane doesn't look at,
    # so a child-context Foreman line loaded from history would otherwise render
    # without its task badge. See docs/foreman-per-task-context.md.
    d["taskId"] = d.pop("task_id", None)
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
# Worker tools whose env vars can be scoped per-tool (passed only to that tool's
# runner, never leaked into the others' environment).
_SCOPED_TOOLS = {"claude", "pi", "codex"}
_MAX_ENV_VALUE_LEN = 4096

# Foreman LLM settings that can be supplied by the process environment. Surfaced
# (masked) on GET so the settings UI can show "from env" instead of an empty
# field when the guild hasn't overridden them.
_FOREMAN_ENV_DEFAULT_KEYS = (
    "FOREMAN_PROVIDER",
    "FOREMAN_MODEL",
    "FOREMAN_BEDROCK_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)


def _mask_env_default(key: str, value: str) -> str:
    """Show non-secret config (provider/model/region/url) as-is; mask secrets to
    the last 4 chars so the UI can confirm one is set without leaking it."""
    if any(tag in key for tag in ("KEY", "TOKEN", "SECRET")):
        return f"••••{value[-4:]}" if len(value) > 4 else "••••"
    return value


def _foreman_env_defaults() -> dict[str, str]:
    return {
        k: _mask_env_default(k, v) for k in _FOREMAN_ENV_DEFAULT_KEYS if (v := os.environ.get(k))
    }


class EnvVarItem(BaseModel):
    key: str
    # None → keep the currently stored value. Kept for API compatibility; the UI
    # now sends actual values since env vars are returned in clear text.
    value: str | None = None
    # Shared env_vars only: True → also forward this var to worker tools. Default
    # (None/False) keeps it with the foreman's own LLM and does NOT leak it to
    # workers. Ignored for tool_env_vars (those are always scoped to their tool).
    forward: bool | None = None


def _validate_env_var_list(v: list[EnvVarItem] | None) -> list[EnvVarItem] | None:
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


def _merge_env_var_list(submitted: list[EnvVarItem], existing: list[dict] | None) -> list[dict]:
    """Merge a submitted env-var list over the stored one.

    - value None → keep the existing stored value (dropped if none stored).
    - forward None → keep the existing stored flag (so a value-only PATCH doesn't
      silently un-forward a var); explicit True/False overrides it.
    - Duplicate keys collapse, preferring a non-empty value over a blank one so a
      stray blank row can't shadow a real credential at spawn time.
    """
    existing_map: dict[str, dict] = {e["key"]: e for e in (existing or [])}
    merged: dict[str, dict] = {}
    for item in submitted:
        if item.value is None:
            if item.key not in existing_map:
                continue
            resolved = existing_map[item.key].get("value", "")
        else:
            resolved = item.value
        forward = item.forward
        if forward is None:
            forward = bool(existing_map.get(item.key, {}).get("forward"))
        if item.key in merged and resolved == "" and merged[item.key]["value"] != "":
            continue
        entry: dict = {"key": item.key, "value": resolved}
        if forward:
            # Only persist the flag when set, keeping unshared entries' shape as
            # {key, value} (back-compat with stored configs and existing tests).
            entry["forward"] = True
        merged[item.key] = entry
    return list(merged.values())


class ForemanConfigUpdate(BaseModel):
    model: str | None = None
    provider: str | None = None
    system_prompt_suffix: str | None = Field(default=None, max_length=10000)
    max_rounds: int | None = Field(default=None, gt=0)
    poll_min_interval: int | None = Field(default=None, gt=0)
    poll_max_interval: int | None = Field(default=None, gt=0)
    # Default model/provider used when the foreman assigns a task to the Pi tool
    # without an explicit override (Pi is provider-agnostic, unlike claude/codex).
    pi_default_model: str | None = Field(default=None, max_length=200)
    pi_default_provider: str | None = Field(default=None, max_length=100)
    # Default model used when the foreman assigns a task to the Codex tool
    # without an explicit override.
    codex_default_model: str | None = Field(default=None, max_length=200)
    # None (field absent) → leave existing env_vars unchanged.
    # Empty list → clear all env_vars.
    # Shared env vars: applied to every worker tool AND the foreman's own LLM.
    env_vars: list[EnvVarItem] | None = None
    # Per-tool env vars: each tool's runner receives only its own set (plus the
    # shared env_vars), so e.g. Pi's Bedrock token never reaches the Claude CLI.
    # None → leave unchanged; a tool mapped to [] clears that tool's vars.
    tool_env_vars: dict[str, list[EnvVarItem]] | None = None

    @field_validator("env_vars")
    @classmethod
    def validate_env_vars(cls, v: list[EnvVarItem] | None) -> list[EnvVarItem] | None:
        return _validate_env_var_list(v)

    @field_validator("tool_env_vars")
    @classmethod
    def validate_tool_env_vars(
        cls, v: dict[str, list[EnvVarItem]] | None
    ) -> dict[str, list[EnvVarItem]] | None:
        if v is None:
            return v
        for tool, items in v.items():
            if tool not in _SCOPED_TOOLS:
                raise ValueError(f"Unknown tool {tool!r}; must be one of {sorted(_SCOPED_TOOLS)}")
            _validate_env_var_list(items)
        return v


class MemberCreate(BaseModel):
    # Either a users.id (numeric GitHub id as text) or a github_login.
    user: str
    role: str = "member"  # owner | member | viewer


class MemberUpdate(BaseModel):
    role: str  # owner | member | viewer


class InviteCreate(BaseModel):
    # GitHub username or numeric GitHub ID of the person to invite.
    user: str
    role: str = "member"  # owner | member | viewer


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
        index_where=text("deleted_at IS NULL"),
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


class GithubAppInstallation(BaseModel):
    # GitHub installation ids are integers; accept the digits from the install
    # URL (github.com/settings/installations/<id>). Empty string clears it.
    installation_id: str = Field(pattern=r"^\d*$")


@router.put("/guilds/{guild_id}/github-app-installation")
async def set_github_app_installation(
    guild_id: str,
    body: GithubAppInstallation,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Set (or clear) the GitHub App installation id for this guild (owner only).

    Find it in the install URL after installing the App on your account/org:
    ``github.com/settings/installations/<id>``. Clearing (empty string) falls
    the guild back to the process-wide ``GITHUB_APP_INSTALLATION_ID`` env var.
    """
    res = await db.exec(select(Guild).where(col(Guild.slug) == guild_id))
    guild = res.one_or_none()
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found")
    guild.github_app_installation_id = body.installation_id or None
    await db.commit()
    return {"slug": guild_id, "github_app_installation_id": guild.github_app_installation_id}


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
    return {**(guild.foreman_config or {}), "env_defaults": _foreman_env_defaults()}


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
                # Collapse duplicate keys (the guild edit screen can submit the
                # same key twice — e.g. a well-known field plus a free-form row),
                # keeping a non-empty value over a blank one so a stray blank
                # can't shadow the real value at spawn time.
                config["env_vars"] = _merge_env_var_list(value, config.get("env_vars"))
        elif field == "tool_env_vars":
            if value is None:
                config.pop("tool_env_vars", None)
            else:
                # Merge each submitted tool independently; tools absent from the
                # payload keep their stored vars. A tool mapped to [] clears it.
                existing_tools: dict = dict(config.get("tool_env_vars") or {})
                for tool, items in value.items():
                    existing_tools[tool] = _merge_env_var_list(items, existing_tools.get(tool))
                config["tool_env_vars"] = existing_tools
        elif value is None:
            config.pop(field, None)
        else:
            config[field] = value

    # Bedrock inference-profile ARNs are AWS-account-scoped, so there is no safe
    # default model to fall back to at run time (see #817). Reject the save now
    # rather than let it fail opaquely against a placeholder/wrong-account ARN.
    if (
        config.get("provider") == "bedrock"
        and not config.get("model")
        and not os.environ.get("FOREMAN_BEDROCK_MODEL")
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Bedrock provider requires a model: set the Model field to an "
                "inference-profile ARN or model ID for your AWS account."
            ),
        )

    guild.foreman_config = config
    await db.commit()

    # Keep the spawn_settings guild baseline in sync with foreman_config's
    # worker-facing slice: resolve_spawn reads spawn_settings, but the guild
    # settings UI still edits foreman_config (until its own cutover). Forwarded
    # env vars, per-tool scoped vars, and the pi provider/model defaults are the
    # bits that reach a worker; the foreman's own credentials stay behind.
    from spawn_config import upsert_spawn_row  # noqa: PLC0415

    worker_env = {
        e["key"]: e["value"]
        for e in (config.get("env_vars") or [])
        if e.get("forward") and e.get("key") and e.get("value") is not None
    }
    worker_tool_env = {
        tool: {
            i["key"]: i["value"]
            for i in (items or [])
            if i.get("key") and i.get("value") is not None
        }
        for tool, items in (config.get("tool_env_vars") or {}).items()
    }
    await upsert_spawn_row(
        db,
        guild.id,
        None,
        env_vars=worker_env,
        tool_env_vars={t: kv for t, kv in worker_tool_env.items() if kv},
        provider=config.get("pi_default_provider"),
        model=config.get("pi_default_model"),
    )
    return config


@router.post("/api/guilds/{guild_id}/invites")
async def create_guild_invite(
    guild_id: str,
    data: InviteCreate,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Create a pending invite for a GitHub user by username or numeric ID (owner only).

    The invite is stored and automatically applied when the target user first logs in.
    If a pending invite already exists for the same identifier, the role is updated.
    """
    if data.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role; must be one of {sorted(_VALID_ROLES)}"
        )
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    identifier = data.user.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="GitHub username or ID is required")

    # Numeric strings are treated as GitHub user IDs; everything else is a username.
    is_numeric = identifier.isdigit()
    github_login = None if is_numeric else identifier.lower()
    github_id_val = identifier if is_numeric else None

    now = datetime.now(UTC)

    # Upsert: update role on an existing pending invite for the same identifier.
    if github_login is not None:
        existing_res = await db.exec(
            select(GuildInvite).where(
                col(GuildInvite.guild_id) == guild_pk,
                col(GuildInvite.github_login) == github_login,
                col(GuildInvite.status) == "pending",
            )
        )
    else:
        existing_res = await db.exec(
            select(GuildInvite).where(
                col(GuildInvite.guild_id) == guild_pk,
                col(GuildInvite.github_id) == github_id_val,
                col(GuildInvite.status) == "pending",
            )
        )
    existing = existing_res.one_or_none()
    if existing:
        existing.role = data.role
        await db.commit()
        await db.refresh(existing)
        return {
            "id": existing.id,
            "guild_id": guild_id,
            "github_login": existing.github_login,
            "github_id": existing.github_id,
            "role": existing.role,
            "created_at": existing.created_at,
            "status": existing.status,
        }

    invite = GuildInvite(
        guild_id=guild_pk,
        github_login=github_login,
        github_id=github_id_val,
        role=data.role,
        invited_by=github_user_id,
        created_at=now,
        status="pending",
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return {
        "id": invite.id,
        "guild_id": guild_id,
        "github_login": invite.github_login,
        "github_id": invite.github_id,
        "role": invite.role,
        "created_at": invite.created_at,
        "status": invite.status,
    }


@router.get("/api/guilds/{guild_id}/invites")
async def list_guild_invites(
    guild_id: str,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """List pending invites for a guild (owner only)."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(GuildInvite)
        .where(
            col(GuildInvite.guild_id) == guild_pk,
            col(GuildInvite.status) == "pending",
        )
        .order_by(col(GuildInvite.created_at).asc())
    )
    return [
        {
            "id": inv.id,
            "github_login": inv.github_login,
            "github_id": inv.github_id,
            "role": inv.role,
            "created_at": inv.created_at,
            "status": inv.status,
        }
        for inv in result.all()
    ]


@router.delete("/api/guilds/{guild_id}/invites/{invite_id}")
async def cancel_guild_invite(
    guild_id: str,
    invite_id: int,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Cancel a pending invite (owner only)."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    res = await db.exec(
        select(GuildInvite).where(
            col(GuildInvite.id) == invite_id,
            col(GuildInvite.guild_id) == guild_pk,
        )
    )
    invite = res.one_or_none()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    invite.status = "cancelled"
    await db.commit()
    return {"status": "cancelled", "id": invite_id}


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
