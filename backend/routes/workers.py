"""Worker registration, container spawn, task assignment, and worker messaging.

Note: a ``Worker`` row is the persistent identity for one running worker
process; an ``Agent`` row is its live WebSocket presence, created only when
the worker process connects via WebSocket (``join`` message in ws_handlers.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import secrets
import string
from datetime import UTC, datetime

import worker_runtime
from auth_deps import ensure_membership, get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg, emit_terminal_line
from fastapi import APIRouter, Depends, HTTPException
from models import VALID_TOOLS, Task, Worker, live_tasks_filter
from pydantic import BaseModel, field_validator
from spawn_config import SpawnLayer, get_spawn_row, resolve_spawn, row_to_layer, upsert_spawn_row
from spawn_profiles import (
    BUILTIN_PROFILES,
    as_layer,
    create_profile,
    delete_profile,
    get_profile_row,
    list_profiles,
    resolve_profile,
    update_profile,
)
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from utils import (
    build_spawn_worker_env,
    mask_secret,
    row_to_dict,
    worker_display_name,
)
from worker_lifecycle import (
    check_worker_spawn_cooldown,
    force_kill_worker_if_unresponsive,
    generate_worker_id,
    record_worker_spawn,
)
from ws_handlers import _resolve_user_identifier
from ws_types import TaskAssignedMsg, WorkerMessageMsg, WorkerShutdownMsg

logger = logging.getLogger(__name__)
router = APIRouter()


class WorkerCreate(BaseModel):
    repos: list[str] = []  # ["owner/repo", ...]
    # Optional GitHub org. When set the worker accepts any task for <org>/* and
    # clones repos lazily. May be used alongside or instead of repos.
    org: str | None = None
    github_token: str | None = None
    hostname: str | None = None
    # Either a users.id (numeric GitHub id as text) or a github_login. The
    # backend resolves it to a User row; mismatches are dropped silently
    # (workers without a known user run as unattributed).
    user: str | None = None


_MAX_ENV_VARS = 20
_MAX_ENV_VALUE_LEN = 4096


class SpawnWorkerRequest(BaseModel):
    # Optional named spawn profile (built-in or guild/user-defined via
    # /spawn-profiles) providing the tool/provider/repos/agent_count
    # defaults for this spawn; any field also set explicitly below overrides
    # the profile's value. See spawn_profiles.py.
    profile: str | None = None
    repos: list[str] = []
    name: str | None = None
    tools: list[str] | None = None
    agent_count: int | None = None
    env_vars: dict[str, str] | None = None
    # Guild-level foreman_config.env_vars keys to drop from this spawn only (the
    # guild's stored credentials are unchanged). Lets an operator see what a
    # worker would inherit and opt individual ones out for a single launch.
    exclude_env_keys: list[str] | None = None

    @field_validator("env_vars")
    @classmethod
    def validate_env_vars(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        if len(v) > _MAX_ENV_VARS:
            raise ValueError(f"Too many env vars (max {_MAX_ENV_VARS})")
        for key, value in v.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ValueError(
                    f"Invalid env var key: {key!r}. Keys must match ^[A-Za-z_][A-Za-z0-9_]*$"
                )
            if len(value) > _MAX_ENV_VALUE_LEN:
                raise ValueError(
                    f"Env var value for {key!r} exceeds max length ({_MAX_ENV_VALUE_LEN} chars)"
                )
        return v

    @field_validator("exclude_env_keys")
    @classmethod
    def validate_exclude_env_keys(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) > _MAX_ENV_VARS:
            raise ValueError(f"Too many excluded env keys (max {_MAX_ENV_VARS})")
        for key in v:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ValueError(
                    f"Invalid env var key: {key!r}. Keys must match ^[A-Za-z_][A-Za-z0-9_]*$"
                )
        return v


class SpawnProfileCreate(BaseModel):
    """Create a named spawn profile, guild-wide or scoped to the caller."""

    name: str
    description: str | None = None
    tool: str = "claude"
    provider: str | None = None
    credentials_source: str | None = None
    default_agent_count: int | None = None
    default_repos: list[str] = []
    # "guild" (default): visible to every guild member. "user": only the
    # caller's own profile, overriding a guild profile of the same name for them.
    scope: str = "guild"

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        return v

    @field_validator("tool")
    @classmethod
    def validate_tool(cls, v: str) -> str:
        if v not in VALID_TOOLS:
            raise ValueError(f"tool must be one of {VALID_TOOLS}")
        return v

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: str) -> str:
        if v not in ("guild", "user"):
            raise ValueError("scope must be 'guild' or 'user'")
        return v

    @field_validator("default_agent_count")
    @classmethod
    def validate_agent_count(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 16):
            raise ValueError("default_agent_count must be between 1 and 16")
        return v


class SpawnProfileUpdate(BaseModel):
    """Partial update — only the fields present in the request body change."""

    name: str | None = None
    description: str | None = None
    tool: str | None = None
    provider: str | None = None
    credentials_source: str | None = None
    default_agent_count: int | None = None
    default_repos: list[str] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("name must not be empty")
        return v.strip() if v is not None else v

    @field_validator("tool")
    @classmethod
    def validate_tool(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_TOOLS:
            raise ValueError(f"tool must be one of {VALID_TOOLS}")
        return v

    @field_validator("default_agent_count")
    @classmethod
    def validate_agent_count(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 16):
            raise ValueError("default_agent_count must be between 1 and 16")
        return v


def _spawn_profile_payload(row) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "tool": row.tool,
        "provider": row.provider,
        "credentials_source": row.credentials_source,
        "default_agent_count": row.default_agent_count,
        "default_repos": json.loads(row.default_repos) if row.default_repos else [],
        "scope": "user" if row.user_id else "guild",
        "builtin": False,
    }


def _builtin_profile_payload(name: str, p) -> dict:
    return {
        "id": name,
        "name": name,
        "description": p.description,
        "tool": p.tool,
        "provider": p.provider,
        "credentials_source": p.credentials_source,
        "default_agent_count": p.default_agent_count,
        "default_repos": list(p.default_repos or ()),
        "scope": "guild",
        "builtin": True,
    }


class SaveSpawnDefaultsRequest(BaseModel):
    """Guild-level spawn baseline. A full-replace PUT (no partial semantics)."""

    repos: list[str] = []
    tools: list[str] = []
    agent_count: int | None = None

    @field_validator("agent_count")
    @classmethod
    def validate_agent_count(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 16):
            raise ValueError("agent_count must be between 1 and 16")
        return v


class TaskCreate(BaseModel):
    description: str
    name: str | None = None
    tool: str = "claude"  # "claude" | "codex" | "pi"
    model: str | None = None
    provider: str | None = None
    issue_number: int | None = None
    issue_repo: str | None = None
    pr_number: int | None = None
    pr_repo: str | None = None
    repos: list[str] = []
    parent_task_id: str | None = None
    phase: str | None = "execute"


class WorkerMessage(BaseModel):
    message: str
    # Optional target task. A worker runs several agents at once, so a message
    # without one is only delivered when exactly one of them is running.
    task_id: str | None = None


@router.post("/guilds/{guild_id}/workers")
async def create_worker(
    guild_id: str,
    data: WorkerCreate,
    db: AsyncSession = Depends(get_db_dep),
):
    """Register a worker agent. The actual worker process must connect via WebSocket
    using the returned id (see the standalone /worker package).

    The response includes an ``auth_token`` the worker must present as a Bearer
    credential when fetching guild secrets (Claude/GitHub creds). The token is
    only returned here — there is no read-after-create endpoint by design, so
    losing it means re-registering."""
    worker_id = await generate_worker_id(db)
    created_at = datetime.now(UTC)
    worker_name = worker_display_name(worker_id, data.hostname)
    auth_token = secrets.token_urlsafe(32)

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    resolved_user_id = await _resolve_user_identifier(db, data.user) if data.user else None
    db.add(
        Worker(
            id=worker_id,
            guild_id=guild_pk,
            repos=json.dumps(data.repos),
            org=data.org,
            state="offline",
            created_at=created_at,
            user_id=resolved_user_id,
            auth_token=auth_token,
            name=worker_name,
            hostname=data.hostname,
        )
    )
    await db.commit()

    return {
        "id": worker_id,
        "name": worker_name,
        "repos": data.repos,
        "org": data.org,
        "created_at": created_at,
        "auth_token": auth_token,
    }


@router.post("/guilds/{guild_id}/spawn-worker")
async def spawn_worker_container(
    guild_id: str,
    data: SpawnWorkerRequest,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Start a new worker container.

    Uses the runtime selected by ``worker_runtime``: the mounted Docker socket
    in docker-compose/local dev, or ECS RunTask when the backend runs on
    Fargate (ECS_CLUSTER_NAME + ECS_WORKER_TASK_DEFINITION set).
    """
    try:
        await worker_runtime.check_runtime_available()
    except worker_runtime.WorkerSpawnError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    remaining = await check_worker_spawn_cooldown(db, guild_pk)
    if remaining is not None:
        minutes = max(1, math.ceil(remaining.total_seconds() / 60))
        raise HTTPException(
            status_code=429,
            detail=(
                "Worker spawn cooldown active. Try again in "
                f"{minutes} minute{'s' if minutes != 1 else ''}."
            ),
        )

    profile_layer = None
    if data.profile:
        profile = await resolve_profile(db, guild_pk, github_user_id, data.profile)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No spawn profile named {data.profile!r} for this guild/user.",
            )
        profile_layer = as_layer(profile)

    # One resolution path (see spawn_config): request args override the
    # profile (if any) override this user's spawn_settings override over the
    # guild baseline. The empty-value guard and the guild/user env merge all
    # live in resolve_spawn now.
    resolved = await resolve_spawn(
        db,
        guild_pk,
        github_user_id,
        SpawnLayer(
            repos=data.repos or None,
            tools=data.tools or None,
            agent_count=data.agent_count,
            env_vars=data.env_vars or None,
        ),
        profile_layer=profile_layer,
    )
    # exclude_env_keys drops specific keys from the resolved worker env for this
    # spawn only (the UI's per-spawn opt-out). A key the caller also set
    # explicitly in this request is a deliberate replacement, not an exclusion,
    # so it survives.
    excluded_keys = sorted(set(data.exclude_env_keys or []) - set(data.env_vars or {}))
    for k in excluded_keys:
        resolved.env_vars.pop(k, None)

    # Pre-register the worker so the container inherits a known worker_id and
    # can skip self-registration on startup.
    worker_id = await generate_worker_id(db)
    auth_token = secrets.token_urlsafe(32)
    worker_name = data.name or worker_display_name(worker_id, None)
    db.add(
        Worker(
            id=worker_id,
            guild_id=guild_pk,
            repos=json.dumps(resolved.repos),
            state="offline",
            created_at=datetime.now(UTC),
            auth_token=auth_token,
            name=worker_name,
            user_id=github_user_id,
            # Recorded so the worker's startup fetch of /foreman/env-vars can't
            # re-supply what this launch opted out of.
            excluded_env_keys=excluded_keys or None,
        )
    )
    await db.commit()
    # TODO: If the process crashes between this commit and the docker run below,
    # the row is left in "offline" state indefinitely (orphaned).  A background
    # job or TTL-based cleanup should set stale "offline"/"pending" rows older
    # than ~5 minutes to "spawn_failed".

    env = build_spawn_worker_env(
        guild_id=guild_id,
        repos=resolved.repos,
        worker_name=worker_name,
        source_env=dict(os.environ),
        worker_id=worker_id,
        auth_token=auth_token,
        agent_count=resolved.agent_count,
        tools=resolved.tools or None,
        extra_env=resolved.env_vars or None,
    )

    try:
        spawned = await worker_runtime.start_worker_container(env=env, guild_id=guild_id)
    except worker_runtime.WorkerSpawnError as e:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to start container: {e}")

    # Persist the spawn handle and version so the lifecycle module can
    # force-kill this container/task if the backend is redeployed at a
    # different version (or the worker idles past the reap timeout).
    await record_worker_spawn(
        db,
        worker_id,
        spawned.handle,
    )

    return {
        "worker_id": worker_id,
        "container_id": spawned.short_id,
        "runtime": spawned.runtime,
    }


@router.get("/guilds/{guild_id}/spawn-credentials")
async def get_spawn_credentials(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return masked credential status for the spawn form — never raw values.

    Covers the guild's forwarded ``foreman_config.env_vars`` (only vars marked
    ``forward=True`` reach a worker; merged into every spawn unless excluded
    via ``exclude_env_keys`` on POST /spawn-worker). Lets an operator see,
    before launching, what a worker will have access to without exposing the
    underlying secret values.

    ``guild_env_vars`` is the **guild baseline only**, deliberately not the
    resolved merge: the spawn form shows the caller's own saved vars separately
    (in clear text, from /spawn-settings) so they stay editable, and needs the
    baseline on its own to mark which of them override a guild credential.
    ``guild_tool_env_vars`` is the same for the guild's per-tool scoped vars.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    baseline = row_to_layer(await get_spawn_row(db, guild_pk, None))
    guild_env_vars = [
        {"key": k, "masked_value": mask_secret(v or "")}
        for k, v in sorted(((baseline.env_vars if baseline else None) or {}).items())
    ]
    guild_tool_env_vars = {
        tool: [{"key": k, "masked_value": mask_secret(v or "")} for k, v in sorted(kv.items())]
        for tool, kv in sorted(((baseline.tool_env_vars if baseline else None) or {}).items())
        if kv
    }
    return {"guild_env_vars": guild_env_vars, "guild_tool_env_vars": guild_tool_env_vars}


@router.post("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def assign_task(
    guild_id: str,
    worker_id: str,
    data: TaskCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Persist a task and broadcast a task-assigned event for the worker process."""
    task_id = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(UTC)

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(Worker.id)).where(col(Worker.id) == worker_id, col(Worker.guild_id) == guild_pk)
    )
    if not result.one_or_none():
        raise HTTPException(status_code=404, detail="Worker not found")
    name = data.name or data.description[:60]
    db.add(
        Task(
            id=task_id,
            worker_id=worker_id,
            guild_id=guild_pk,
            name=name,
            description=data.description,
            tool=data.tool,
            model=data.model,
            provider=data.provider,
            issue_number=data.issue_number,
            issue_repo=data.issue_repo,
            pr_number=data.pr_number,
            pr_repo=data.pr_repo,
            state="pending",
            phase=data.phase or "execute",
            parent_task_id=data.parent_task_id,
            created_at=created_at,
            user_id=github_user_id,
        )
    )
    await db.commit()

    await broadcast_msg(
        guild_id,
        TaskAssignedMsg(
            workerId=worker_id,
            taskId=task_id,
            name=name,
            description=data.description,
            tool=data.tool,
            model=data.model,
            provider=data.provider,
            phase=data.phase or "execute",
            parentTaskId=data.parent_task_id,
            issueNumber=data.issue_number,
            issueRepo=data.issue_repo,
            prNumber=data.pr_number,
            prRepo=data.pr_repo,
            repos=data.repos,
        ),
    )

    return {"id": task_id, "worker_id": worker_id, "state": "pending"}


@router.get("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def list_tasks(
    guild_id: str,
    worker_id: str,
    db: AsyncSession = Depends(get_db_dep),
):
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(Task)
        .where(
            col(Task.worker_id) == worker_id,
            col(Task.guild_id) == guild_pk,
            live_tasks_filter(),
        )
        .order_by(col(Task.created_at).desc())
    )
    return [row_to_dict(t) for t in result.all()]


_MAX_SETTINGS_ENV_VARS = 50
_MAX_SETTINGS_ENV_KEY_LEN = 256
_MAX_SETTINGS_ENV_VALUE_LEN = 1024


class EnvVarPair(BaseModel):
    key: str
    value: str


# The worker tools that can carry a per-tool env override, mirroring the guild
# settings Worker Tools tabs. Unknown tool keys are rejected so the stored map
# can't grow unbounded from a malformed payload.
_SPAWN_TOOLS = ("claude", "pi", "codex")


def _validate_env_pairs(pairs: list[EnvVarPair]) -> list[EnvVarPair]:
    if len(pairs) > _MAX_SETTINGS_ENV_VARS:
        raise ValueError(f"Too many env vars (max {_MAX_SETTINGS_ENV_VARS})")
    for pair in pairs:
        if len(pair.key) > _MAX_SETTINGS_ENV_KEY_LEN:
            raise ValueError(f"Env var key exceeds max length ({_MAX_SETTINGS_ENV_KEY_LEN} chars)")
        if len(pair.value) > _MAX_SETTINGS_ENV_VALUE_LEN:
            raise ValueError("An env var value exceeds the maximum allowed length")
        if pair.key and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", pair.key):
            raise ValueError(
                f"Invalid env var key: {pair.key!r}. Must match ^[A-Za-z_][A-Za-z0-9_]*$"
            )
    return pairs


class SaveSpawnSettingsRequest(BaseModel):
    # All fields default to empty so a PUT always replaces the full settings object
    # (no partial-update / PATCH semantics).
    repos: list[str] = []
    tools: list[str] = []
    envVars: list[EnvVarPair] = []
    # Per-tool env overrides, {tool: [{key, value}, ...]}. Same shape as the guild
    # settings tool_env_vars; merged in only when that tool spawns for this user.
    toolEnvVars: dict[str, list[EnvVarPair]] = {}

    @field_validator("envVars")
    @classmethod
    def validate_env_var_keys(cls, v: list[EnvVarPair]) -> list[EnvVarPair]:
        return _validate_env_pairs(v)

    @field_validator("toolEnvVars")
    @classmethod
    def validate_tool_env_vars(cls, v: dict[str, list[EnvVarPair]]) -> dict[str, list[EnvVarPair]]:
        for tool, pairs in v.items():
            if tool not in _SPAWN_TOOLS:
                raise ValueError(f"Unknown tool {tool!r}; expected one of {_SPAWN_TOOLS}")
            _validate_env_pairs(pairs)
        return v


@router.get("/guilds/{guild_id}/spawn-settings")
async def get_spawn_settings(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the current user's saved spawn-worker settings for this guild."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    row = await get_spawn_row(db, guild_pk, github_user_id)
    if row is None:
        return {}
    return {
        "repos": json.loads(row.repos or "[]"),
        "tools": json.loads(row.tools or "[]"),
        "envVars": [{"key": k, "value": v} for k, v in (row.env_vars or {}).items()],
        "toolEnvVars": {
            tool: [{"key": k, "value": v} for k, v in (kv or {}).items()]
            for tool, kv in (row.tool_env_vars or {}).items()
        },
    }


@router.put("/guilds/{guild_id}/spawn-settings")
async def save_spawn_settings(
    guild_id: str,
    data: SaveSpawnSettingsRequest,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Persist the current user's spawn-worker settings for this guild."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    # Full-replace PUT semantics: every field is written, so this is the user's
    # complete override row for the guild.
    # TODO: encrypt env var values at rest before production use (tracked in GH issue #537).
    env_vars = {p.key: p.value for p in data.envVars if p.key}
    tool_env_vars = {
        tool: {p.key: p.value for p in pairs if p.key} for tool, pairs in data.toolEnvVars.items()
    }
    # Drop tools that resolved to no vars so the stored map stays clean.
    tool_env_vars = {t: kv for t, kv in tool_env_vars.items() if kv}
    await upsert_spawn_row(
        db,
        guild_pk,
        github_user_id,
        repos=json.dumps(data.repos),
        tools=json.dumps(data.tools),
        env_vars=env_vars,
        tool_env_vars=tool_env_vars,
    )
    return {"status": "saved"}


def _spawn_defaults_payload(row) -> dict:
    """Serialise a spawn_settings baseline row (or None) to the wire shape."""
    if row is None:
        return {"repos": [], "tools": [], "agent_count": None}
    return {
        "repos": json.loads(row.repos or "[]"),
        "tools": json.loads(row.tools or "[]"),
        "agent_count": row.agent_count,
    }


@router.get("/guilds/{guild_id}/spawn-defaults")
async def get_spawn_defaults(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the guild's default spawn parameters (repos, tools, agent_count).

    Readable by any guild member: the per-user spawn form uses these as the
    baseline it lets the caller override. Returns empty defaults when the guild
    has never recorded a spawn and no owner has set them explicitly.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    row = await get_spawn_row(db, guild_pk, None)
    return _spawn_defaults_payload(row)


@router.put("/guilds/{guild_id}/spawn-defaults")
async def save_spawn_defaults(
    guild_id: str,
    data: SaveSpawnDefaultsRequest,
    github_user_id: str = Depends(require_member("owner")),
    db: AsyncSession = Depends(get_db_dep),
):
    """Set the guild's default spawn parameters (owner only).

    These become the baseline the foreman's ``spawn_worker`` tool falls back to
    (via ``resolve_spawn``) and the per-user spawn form pre-fills from. Worker
    spawns no longer write defaults back; this endpoint is the owner's curation
    path for the guild baseline row.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    row = await upsert_spawn_row(
        db,
        guild_pk,
        None,
        repos=json.dumps(data.repos),
        tools=json.dumps(data.tools),
        agent_count=data.agent_count,
    )
    return _spawn_defaults_payload(row)


@router.get("/guilds/{guild_id}/spawn-profiles")
async def list_spawn_profiles(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """List profiles selectable by this caller: built-ins, the guild's custom

    profiles, and the caller's own. A user-scoped profile shadows a
    guild-scoped one of the same name (matching ``spawn_profiles.resolve_profile``).
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    rows = await list_profiles(db, guild_pk, github_user_id)
    by_name = {name: _builtin_profile_payload(name, p) for name, p in BUILTIN_PROFILES.items()}
    for row in rows:
        by_name[row.name] = _spawn_profile_payload(row)
    return sorted(by_name.values(), key=lambda p: p["name"])


@router.post("/guilds/{guild_id}/spawn-profiles")
async def create_spawn_profile(
    guild_id: str,
    data: SpawnProfileCreate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Create a custom spawn profile.

    Guild-scoped profiles (``scope: "guild"``, the default) require the owner
    role, since they're visible to and usable by every member. A caller may
    always create their own ``scope: "user"`` profile.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    if data.scope == "guild":
        role = await ensure_membership(db, guild_id, github_user_id)
        if role != "owner":
            raise HTTPException(
                status_code=403,
                detail="Creating a guild-wide spawn profile requires the owner role",
            )
    user_id = github_user_id if data.scope == "user" else None
    try:
        row = await create_profile(
            db,
            guild_pk,
            user_id,
            name=data.name,
            description=data.description,
            tool=data.tool,
            provider=data.provider,
            credentials_source=data.credentials_source,
            default_agent_count=data.default_agent_count,
            default_repos=data.default_repos,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A {data.scope} spawn profile named {data.name!r} already exists.",
        )
    return _spawn_profile_payload(row)


@router.patch("/guilds/{guild_id}/spawn-profiles/{profile_id}")
async def update_spawn_profile(
    guild_id: str,
    profile_id: str,
    data: SpawnProfileUpdate,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Update a custom spawn profile. Built-in profiles are not rows and 404 here."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    row = await get_profile_row(db, guild_pk, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Spawn profile not found")
    if row.user_id is None:
        role = await ensure_membership(db, guild_id, github_user_id)
        if role != "owner":
            raise HTTPException(
                status_code=403,
                detail="Modifying a guild-wide spawn profile requires the owner role",
            )
    elif row.user_id != github_user_id:
        raise HTTPException(status_code=403, detail="Not your spawn profile")
    fields = {k: getattr(data, k) for k in data.model_fields_set}
    try:
        row = await update_profile(db, row, **fields)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="A spawn profile with that name already exists."
        )
    return _spawn_profile_payload(row)


@router.delete("/guilds/{guild_id}/spawn-profiles/{profile_id}")
async def delete_spawn_profile(
    guild_id: str,
    profile_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Delete a custom spawn profile. Built-in profiles are not rows and 404 here."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    row = await get_profile_row(db, guild_pk, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Spawn profile not found")
    if row.user_id is None:
        role = await ensure_membership(db, guild_id, github_user_id)
        if role != "owner":
            raise HTTPException(
                status_code=403,
                detail="Modifying a guild-wide spawn profile requires the owner role",
            )
    elif row.user_id != github_user_id:
        raise HTTPException(status_code=403, detail="Not your spawn profile")
    await delete_profile(db, row)
    return {"status": "deleted"}


@router.post("/guilds/{guild_id}/workers/{worker_id}/message")
async def message_worker(
    guild_id: str,
    worker_id: str,
    data: WorkerMessage,
    github_user_id: str = Depends(require_member()),
):
    """Forward a message to a worker process via its guild WebSocket."""
    text_msg = data.message.strip()
    if not text_msg:
        raise HTTPException(status_code=400, detail="Empty message")

    await emit_terminal_line(guild_id, worker_id, f"[foreman → worker] {text_msg}")
    await broadcast_msg(
        guild_id,
        WorkerMessageMsg(workerId=worker_id, message=text_msg, taskId=data.task_id),
    )
    # "sent", not "delivered": the worker decides whether a running agent matches.
    return {"status": "sent"}


@router.post("/guilds/{guild_id}/workers/{worker_id}/shutdown")
async def shutdown_worker_endpoint(
    guild_id: str,
    worker_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    """Gracefully shut down a worker process (operator-initiated).

    Mirrors the foreman ``shutdown_worker`` tool so the frontend button drives the
    real shutdown path instead of injecting an English message into a running
    agent: broadcast the ``worker-shutdown`` signal (which the worker turns into a
    graceful ``_initiate_shutdown`` — idle agents stop, busy agents finish their
    current task, then the process exits), mark the worker ``disabled`` so it is
    not respawned on the next backend restart, and schedule a force-kill backstop
    in case the worker never goes offline on its own.
    """
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    worker = (
        await db.exec(
            select(col(Worker.id)).where(
                col(Worker.id) == worker_id, col(Worker.guild_id) == guild_pk
            )
        )
    ).one_or_none()
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")

    await broadcast_msg(
        guild_id,
        WorkerShutdownMsg(workerId=worker_id, reason="operator-initiated shutdown"),
    )
    await db.exec(update(Worker).where(col(Worker.id) == worker_id).values(disabled=True))
    await db.commit()
    await emit_terminal_line(guild_id, worker_id, "[operator] graceful shutdown signal sent")
    # Force-kill the container only if it's still not offline after the timeout —
    # see worker_lifecycle.force_kill_worker_if_unresponsive.
    spawn(
        force_kill_worker_if_unresponsive(worker_id),
        name=f"shutdown-escalate:{worker_id}",
    )
    return {"status": "shutdown-signalled"}
