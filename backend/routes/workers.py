"""Worker registration, container spawn, task assignment, and worker messaging.

Note: a ``Worker`` row is the persistent identity for one running worker
process; an ``Agent`` row is its live WebSocket presence, created only when
the worker process connects via WebSocket (``join`` message in ws_handlers.py).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import secrets
import string
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member
from database import get_db_dep
from events import broadcast_msg, emit_terminal_line, pending_claude_auth
from fastapi import APIRouter, Depends, HTTPException
from models import ClaudeCredentials, Guild, Task, UserSpawnSettings, Worker, live_tasks_filter
from pydantic import BaseModel, field_validator
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from util.tasks import spawn
from utils import (
    build_spawn_worker_env,
    decode_claude_oauth_token,
    row_to_dict,
    worker_display_name,
)
from worker_lifecycle import (
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
    repos: list[str]
    name: str | None = None
    tools: list[str] | None = None
    agent_count: int | None = None
    env_vars: dict[str, str] | None = None

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
    """Start a new worker container via Docker. Requires the Docker socket to be mounted."""
    try:
        import docker as docker_sdk
    except ImportError:
        raise HTTPException(status_code=503, detail="Docker SDK not installed in backend")

    try:
        client = docker_sdk.from_env()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Docker socket unavailable: {e}")

    image = os.environ.get("WORKER_IMAGE", "pioneer-square-worker")

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(col(ClaudeCredentials.credentials_blob)).where(
            col(ClaudeCredentials.guild_id) == guild_pk
        )
    )
    stored_blob = result.one_or_none()

    # Merge guild-level foreman env vars (base) with user-supplied env vars (override).
    guild_cfg_res = await db.exec(
        select(col(Guild.foreman_config)).where(col(Guild.id) == guild_pk)
    )
    guild_cfg = guild_cfg_res.one_or_none() or {}
    foreman_env_vars: dict[str, str] = {
        e["key"]: e["value"]
        for e in (guild_cfg.get("env_vars") or [])
        if e.get("key") and e.get("value") is not None
    }
    # User-supplied vars at spawn time override guild defaults.
    extra_env: dict[str, str] = {**foreman_env_vars, **(data.env_vars or {})}

    # Pre-register the worker so the container inherits a known worker_id and
    # can skip self-registration on startup.
    worker_id = await generate_worker_id(db)
    auth_token = secrets.token_urlsafe(32)
    worker_name = data.name or worker_display_name(worker_id, None)
    db.add(
        Worker(
            id=worker_id,
            guild_id=guild_pk,
            repos=json.dumps(data.repos),
            state="offline",
            created_at=datetime.now(UTC),
            auth_token=auth_token,
            name=worker_name,
        )
    )
    await db.commit()
    # TODO: If the process crashes between this commit and the docker run below,
    # the row is left in "offline" state indefinitely (orphaned).  A background
    # job or TTL-based cleanup should set stale "offline"/"pending" rows older
    # than ~5 minutes to "spawn_failed".

    env = build_spawn_worker_env(
        guild_id=guild_id,
        repos=data.repos,
        worker_name=worker_name,
        source_env=dict(os.environ),
        claude_oauth_token=decode_claude_oauth_token(stored_blob),
        worker_id=worker_id,
        auth_token=auth_token,
        agent_count=data.agent_count,
        tools=data.tools or None,
        extra_env=extra_env or None,
    )

    # Join the same Docker network as the backend so the worker can reach it.
    network = None
    try:
        me = client.containers.get(os.environ.get("HOSTNAME", ""))
        network = next(iter(me.attrs["NetworkSettings"]["Networks"].keys()), None)
    except Exception:
        pass

    # Pioneer-owned labels — these containers are spawned and lifecycle-managed
    # by the backend, not compose, so we don't tag them with com.docker.compose.*.
    # List them with: docker ps --filter label=com.pioneer.kind=worker
    labels = {
        "com.pioneer.kind": "worker",
        "com.pioneer.guild": guild_id,
    }

    container_name = f"pioneer-worker-{guild_id}-{secrets.token_hex(3)}"

    try:
        run_kwargs: dict = dict(
            image=image,
            environment=env,
            detach=True,
            remove=True,
            labels=labels,
            name=container_name,
        )
        if network:
            run_kwargs["network"] = network
        container = client.containers.run(**run_kwargs)
    except docker_sdk.errors.ImageNotFound:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        raise HTTPException(
            status_code=404,
            detail=f"Worker image '{image}' not found — run: docker compose build worker",
        )
    except Exception as e:
        await db.exec(
            update(Worker).where(col(Worker.id) == worker_id).values(state="spawn_failed")
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to start container: {e}")

    # Persist container id and spawned version so the lifecycle module can
    # force-kill this container if the backend is redeployed at a different version.
    await record_worker_spawn(db, worker_id, container.id)

    return {"worker_id": worker_id, "container_id": container.id[:12], "image": image}


@router.get("/guilds/{guild_id}/pending-auth")
async def get_pending_auth(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
):
    """Return workers currently waiting for a Claude auth code.

    The frontend calls this on mount so the auth panel is restored after a
    page refresh even if the original claude-auth-required broadcast was missed.
    """
    pending = pending_claude_auth.get(guild_id, {})
    return [{"workerId": wid, "url": url} for wid, url in pending.items()]


@router.get("/guilds/{guild_id}/workers")
async def list_workers(
    guild_id: str,
    github_user_id: str = Depends(require_member()),
    db: AsyncSession = Depends(get_db_dep),
):
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    result = await db.exec(
        select(Worker)
        .where(col(Worker.guild_id) == guild_pk)
        .order_by(col(Worker.created_at).desc())
    )
    return [row_to_dict(w) for w in result.all()]


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


class SaveSpawnSettingsRequest(BaseModel):
    # All fields default to empty so a PUT always replaces the full settings object
    # (no partial-update / PATCH semantics).
    repos: list[str] = []
    tools: list[str] = []
    envVars: list[EnvVarPair] = []

    @field_validator("envVars")
    @classmethod
    def validate_env_var_keys(cls, v: list[EnvVarPair]) -> list[EnvVarPair]:
        if len(v) > _MAX_SETTINGS_ENV_VARS:
            raise ValueError(f"Too many env vars (max {_MAX_SETTINGS_ENV_VARS})")
        for pair in v:
            if len(pair.key) > _MAX_SETTINGS_ENV_KEY_LEN:
                raise ValueError(
                    f"Env var key exceeds max length ({_MAX_SETTINGS_ENV_KEY_LEN} chars)"
                )
            if len(pair.value) > _MAX_SETTINGS_ENV_VALUE_LEN:
                raise ValueError("An env var value exceeds the maximum allowed length")
            if pair.key and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", pair.key):
                raise ValueError(
                    f"Invalid env var key: {pair.key!r}. Must match ^[A-Za-z_][A-Za-z0-9_]*$"
                )
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
    result = await db.exec(
        select(UserSpawnSettings).where(
            col(UserSpawnSettings.guild_id) == guild_pk,
            col(UserSpawnSettings.user_id) == github_user_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        return {}
    try:
        return json.loads(row.settings_json)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Corrupt settings_json for user %s guild %s: %s", github_user_id, guild_pk, exc
        )
        return {}


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
    result = await db.exec(
        select(UserSpawnSettings).where(
            col(UserSpawnSettings.guild_id) == guild_pk,
            col(UserSpawnSettings.user_id) == github_user_id,
        )
    )
    row = result.one_or_none()
    # model_dump() without exclude_none gives full-replace PUT semantics: every field
    # is always written, so callers cannot do partial updates through this endpoint.
    incoming = data.model_dump()
    now = datetime.now(UTC)
    # TODO: encrypt env var values at rest before production use (tracked in GH issue #537).
    # Currently stored as plaintext JSON — an improvement over localStorage but not
    # suitable for highly sensitive credentials until encryption is added.
    settings_blob = json.dumps(incoming)
    if row is None:
        db.add(
            UserSpawnSettings(
                guild_id=guild_pk,
                user_id=github_user_id,
                settings_json=settings_blob,
                updated_at=now,
            )
        )
    else:
        row.settings_json = settings_blob
        row.updated_at = now
    await db.commit()
    return {"status": "saved"}


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
    await broadcast_msg(guild_id, WorkerMessageMsg(workerId=worker_id, message=text_msg))
    return {"status": "delivered"}


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
