"""Worker registration, container spawn, task assignment, and worker messaging.

Note: a ``Worker`` row is the persistent identity for one running worker
process; an ``Agent`` row is its live WebSocket presence, created only when
the worker process connects via WebSocket (``join`` message in ws_handlers.py).
"""

from __future__ import annotations

import json
import os
import random
import secrets
import string
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member
from database import get_db
from events import broadcast, emit_terminal_line, pending_claude_auth
from fastapi import APIRouter, Depends, HTTPException
from models import ClaudeCredentials, Task, Worker, live_tasks_filter
from pydantic import BaseModel
from sqlalchemy import select
from utils import (
    build_spawn_worker_env,
    decode_claude_oauth_token,
    row_to_dict,
    worker_display_name,
)
from ws_handlers import _resolve_user_identifier

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


class SpawnWorkerRequest(BaseModel):
    repos: list[str]
    name: str | None = None


class TaskCreate(BaseModel):
    description: str
    name: str | None = None
    tool: str = "claude"  # "claude" | "codex" | "pi"
    issue_number: int | None = None
    issue_repo: str | None = None
    parent_task_id: str | None = None
    phase: str | None = "execute"


class WorkerMessage(BaseModel):
    message: str


@router.post("/guilds/{guild_id}/workers")
async def create_worker(guild_id: str, data: WorkerCreate):
    """Register a worker agent. The actual worker process must connect via WebSocket
    using the returned id (see the standalone /worker package).

    The response includes an ``auth_token`` the worker must present as a Bearer
    credential when fetching guild secrets (Claude/GitHub creds). The token is
    only returned here — there is no read-after-create endpoint by design, so
    losing it means re-registering."""
    worker_id = "w-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(UTC).isoformat()
    worker_name = worker_display_name(worker_id, data.hostname)
    auth_token = secrets.token_urlsafe(32)

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        resolved_user_id = await _resolve_user_identifier(db, data.user) if data.user else None
        db.add(
            Worker(
                id=worker_id,
                guild_pk=guild_pk,
                repos=json.dumps(data.repos),
                org=data.org,
                state="offline",
                created_at=created_at,
                user_id=resolved_user_id,
                auth_token=auth_token,
            )
        )
        await db.commit()
    finally:
        await db.close()

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

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(ClaudeCredentials.credentials_blob).where(ClaudeCredentials.guild_pk == guild_pk)
        )
        stored_blob = result.scalar_one_or_none()
    finally:
        await db.close()

    env = build_spawn_worker_env(
        guild_id=guild_id,
        repos=data.repos,
        worker_name=data.name,
        source_env=dict(os.environ),
        claude_oauth_token=decode_claude_oauth_token(stored_blob),
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
        raise HTTPException(
            status_code=404,
            detail=f"Worker image '{image}' not found — run: docker compose build worker",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start container: {e}")

    return {"container_id": container.id[:12], "image": image}


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
):
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Worker).where(Worker.guild_pk == guild_pk).order_by(Worker.created_at.desc())
        )
        return [row_to_dict(w) for w in result.scalars().all()]
    finally:
        await db.close()


@router.post("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def assign_task(
    guild_id: str,
    worker_id: str,
    data: TaskCreate,
    github_user_id: str = Depends(require_member()),
):
    """Persist a task and broadcast a task-assigned event for the worker process."""
    task_id = "t-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    created_at = datetime.now(UTC).isoformat()

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Worker.id).where(Worker.id == worker_id, Worker.guild_pk == guild_pk)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Worker not found")
        name = data.name or data.description[:60]
        db.add(
            Task(
                id=task_id,
                worker_id=worker_id,
                guild_pk=guild_pk,
                name=name,
                description=data.description,
                tool=data.tool,
                issue_number=data.issue_number,
                issue_repo=data.issue_repo,
                state="pending",
                phase=data.phase or "execute",
                parent_task_id=data.parent_task_id,
                created_at=created_at,
            )
        )
        await db.commit()
    finally:
        await db.close()

    await broadcast(
        guild_id,
        {
            "type": "task-assigned",
            "workerId": worker_id,
            "taskId": task_id,
            "name": name,
            "description": data.description,
            "tool": data.tool,
            "phase": data.phase or "execute",
            "parentTaskId": data.parent_task_id,
            "issueNumber": data.issue_number,
            "issueRepo": data.issue_repo,
        },
    )

    return {"id": task_id, "worker_id": worker_id, "state": "pending"}


@router.get("/guilds/{guild_id}/workers/{worker_id}/tasks")
async def list_tasks(guild_id: str, worker_id: str):
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(Task)
            .where(
                Task.worker_id == worker_id,
                Task.guild_pk == guild_pk,
                live_tasks_filter(),
            )
            .order_by(Task.created_at.desc())
        )
        return [row_to_dict(t) for t in result.scalars().all()]
    finally:
        await db.close()


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
    await broadcast(
        guild_id,
        {
            "type": "worker-message",
            "workerId": worker_id,
            "message": text_msg,
        },
    )
    return {"status": "delivered"}
