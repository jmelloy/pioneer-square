"""Pioneer Square backend entry point.

Routes live in ``routes/*``; OAuth helpers in ``oauth.py``; auth dependencies
in ``auth_deps.py``; the agent subprocess streamer in ``agent_runner.py``.
This module wires them onto a single FastAPI app and runs the lifespan-scoped
background tasks (stale-worker sweeper).

Several private names are re-exported at the bottom for backward compatibility
with tests that ``from main import …`` them directly.
"""

from __future__ import annotations

import asyncio
import logging
import logging.config
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from database import AsyncSessionLocal
from events import agent_owners, broadcast, pending_worker_probes, send_ws_message
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from lock_service import LockService
from models import Agent, Guild, Lock, Task, Worker
from sqlalchemy import literal, update
from sqlmodel import col, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from util.tasks import spawn
from ws_types import WorkerPingMsg

# Load .env (looked up from CWD upward, then alongside this file) before any
# code reads os.environ, so ANTHROPIC_API_KEY etc. are available.
try:
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

# Apply logging config at module import time so no messages are lost before
# the lifespan hook fires.  uvicorn will later call dictConfig with its own
# defaults; our lifespan re-applies it (with force-equivalent disable_existing
# behaviour) to ensure consistent formatting after uvicorn starts.
from logging_config import get_logging_config as _get_logging_config

_early_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
_early_log_format = os.environ.get("LOG_FORMAT", "colored")
logging.config.dictConfig(_get_logging_config(_early_log_level, _early_log_format))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Liveness / sweeper config
# ---------------------------------------------------------------------------

# How long a worker can go without sending any WebSocket message before the
# sweeper probes it. If the worker does not answer the probe, the next sweep
# marks it and its agents offline.
WORKER_OFFLINE_AFTER_SECONDS = float(os.environ.get("WORKER_OFFLINE_AFTER_SECONDS", "90"))
# How long an unanswered worker-ping is allowed to sit before the worker is
# considered offline. The sweeper checks this on its normal interval, so the
# practical delay is this timeout plus up to WORKER_SWEEP_INTERVAL_SECONDS.
WORKER_PROBE_TIMEOUT_SECONDS = float(os.environ.get("WORKER_PROBE_TIMEOUT_SECONDS", "10"))
# How often the sweeper task wakes up to look for stale agents.
WORKER_SWEEP_INTERVAL_SECONDS = float(os.environ.get("WORKER_SWEEP_INTERVAL_SECONDS", "30"))


async def reset_connection_state() -> None:
    # On every startup, no worker processes are connected yet.
    async with AsyncSessionLocal() as db:
        await db.exec(update(Worker).values(state="offline"))
        await db.exec(
            update(Agent)
            .where(
                col(Agent.worker_id).in_(
                    select(col(Worker.id)).where(col(Worker.state) == "offline")
                )
            )
            .values(state="offline", activity=None, current_task_id=None)
        )
        await db.commit()


async def _sweep_stale_workers_once() -> int:
    """One stale-worker sweep pass. Returns workers/agent-only presences marked offline."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=WORKER_OFFLINE_AFTER_SECONDS)
    probe_cutoff = now - timedelta(seconds=WORKER_PROBE_TIMEOUT_SECONDS)
    workers_marked_offline: list[Any] = []
    agents_marked_offline: list[Any] = []
    async with AsyncSessionLocal() as db:
        stale_workers = (
            await db.exec(
                select(
                    col(Worker.id),
                    col(Worker.guild_id),
                    col(Guild.slug).label("guild_slug"),
                    col(Worker.last_seen),
                )
                .join(Guild, col(Guild.id) == col(Worker.guild_id))
                .where(col(Worker.state) != "offline")
                .where(col(Worker.last_seen).isnot(None))
                .where(col(Worker.last_seen) < cutoff)
            )
        ).all()

        # Agent rows that do not belong to a worker still use their own timestamp
        # as the liveness source. Worker-owned agents are taken offline only when
        # their parent worker fails the active probe below.
        stale_agent_only_rows = (
            await db.exec(
                select(
                    col(Agent.id),
                    col(Agent.guild_id),
                    col(Guild.slug).label("guild_slug"),
                )
                .join(Guild, col(Guild.id) == col(Agent.guild_id))
                .where(col(Agent.state) != "offline")
                .where(col(Agent.worker_id).is_(None))
                .where(col(Agent.last_seen).isnot(None))
                .where(col(Agent.last_seen) < cutoff)
            )
        ).all()

        for row in stale_agent_only_rows:
            await db.exec(
                update(Agent)
                .where(col(Agent.id) == row.id, col(Agent.guild_id) == row.guild_id)
                .values(state="offline", activity=None, current_task_id=None)
            )
            agent_owners.pop(row.id, None)
            agents_marked_offline.append(row)

        for row in stale_workers:
            key = (row.guild_id, row.id)
            agent_ids = (
                await db.exec(
                    select(col(Agent.id)).where(
                        col(Agent.worker_id) == row.id,
                        col(Agent.guild_id) == row.guild_id,
                        col(Agent.state) != "offline",
                    )
                )
            ).all()
            owner_ws = next(
                (ws for agent_id in agent_ids if (ws := agent_owners.get(agent_id)) is not None),
                None,
            )
            pending_since = pending_worker_probes.get(key)
            should_mark_offline = False

            if owner_ws is None:
                should_mark_offline = True
            elif pending_since is not None and pending_since < probe_cutoff:
                should_mark_offline = True
            elif pending_since is None:
                try:
                    await send_ws_message(
                        owner_ws,
                        WorkerPingMsg(workerId=row.id, timestamp=now.isoformat()),
                    )
                    pending_worker_probes[key] = now
                    logger.info(
                        "Sent liveness probe to worker %s after %.0fs without inbound WS frame"
                        " (guild=%s)",
                        row.id,
                        WORKER_OFFLINE_AFTER_SECONDS,
                        row.guild_slug,
                    )
                except Exception:
                    logger.warning(
                        "Worker liveness probe failed for %s (guild=%s); marking offline",
                        row.id,
                        row.guild_slug,
                        exc_info=True,
                    )
                    should_mark_offline = True

            if not should_mark_offline:
                continue

            workers_marked_offline.append(row)
            pending_worker_probes.pop(key, None)
            worker_agent_rows = (
                await db.exec(
                    select(
                        col(Agent.id),
                        col(Agent.guild_id),
                        col(Guild.slug).label("guild_slug"),
                    )
                    .join(Guild, col(Guild.id) == col(Agent.guild_id))
                    .where(
                        col(Agent.worker_id) == row.id,
                        col(Agent.guild_id) == row.guild_id,
                        col(Agent.state) != "offline",
                    )
                )
            ).all()
            for agent_row in worker_agent_rows:
                await db.exec(
                    update(Agent)
                    .where(col(Agent.id) == agent_row.id, col(Agent.guild_id) == row.guild_id)
                    .values(state="offline", activity=None, current_task_id=None)
                )
                agent_owners.pop(agent_row.id, None)
                agents_marked_offline.append(agent_row)

            await db.exec(
                update(Worker)
                .where(col(Worker.id) == row.id, col(Worker.guild_id) == row.guild_id)
                .values(state="offline")
            )

        if agents_marked_offline or workers_marked_offline:
            await db.commit()

    async with AsyncSessionLocal() as db:
        expired = await LockService.cleanup_expired(db)
        if expired:
            await db.commit()
            logger.info("Cleared %d expired lock(s)", expired)

    # Stale-task watchdog: find tasks stuck in "working" with no active agent.
    # This catches the case where an agent crashed or disconnected without
    # sending task-followup-done, leaving the task and lock orphaned.  We gate
    # on lock age (>= WORKER_OFFLINE_AFTER_SECONDS) to avoid false positives
    # on tasks that were just dispatched a moment ago.
    stale_task_ids: list[str] = []
    async with AsyncSessionLocal() as db:
        cutoff_lock = datetime.now(UTC) - timedelta(seconds=WORKER_OFFLINE_AFTER_SECONDS)
        # Tasks in "working" state that have no agent actively running them
        # (i.e. no agent with current_task_id = task.id in a live state) and
        # whose lock was acquired long enough ago to not be a new dispatch.
        orphaned = (
            await db.exec(
                select(col(Task.id)).where(
                    col(Task.state) == "working",
                    # Lock exists for this task and has been held long enough
                    # that we can assume it's not a brand-new dispatch.
                    # Use the || operator for string concat — SQLite does not
                    # have a concat() function; this is dialect-neutral for
                    # single-DB deployments and avoids func.concat().
                    select(col(Lock.key))
                    .where(
                        col(Lock.key) == literal("task:").op("||")(col(Task.id)),
                        col(Lock.acquired_at) < cutoff_lock,
                    )
                    .exists(),
                    # No agent is actively running this task right now.
                    ~select(col(Agent.id))
                    .where(
                        col(Agent.current_task_id) == col(Task.id),
                        col(Agent.state).in_(("working", "thinking", "busy")),
                    )
                    .exists(),
                )
            )
        ).all()
        if orphaned:
            stale_task_ids = list(orphaned)
            for task_id in stale_task_ids:
                await db.exec(
                    update(Task)
                    .where(col(Task.id) == task_id, col(Task.state) == "working")
                    .values(state="awaiting-review")
                )
                await LockService(db).release(f"task:{task_id}")
            await db.commit()

    for task_id in stale_task_ids:
        logger.warning(
            "Stale-task watchdog: released lock and moved task %s to awaiting-review"
            " (no active agent for >= %.0fs)",
            task_id,
            WORKER_OFFLINE_AFTER_SECONDS,
        )

    for row in agents_marked_offline:
        logger.warning(
            "Marking %s offline: no live parent worker or agent frame in over %.0fs (guild=%s)",
            row.id,
            WORKER_OFFLINE_AFTER_SECONDS,
            row.guild_slug,
        )
        await broadcast(
            row.guild_slug,
            {"type": "agent-state", "agentId": row.id, "state": "offline"},
        )
    for row in workers_marked_offline:
        logger.warning(
            "Marking worker %s offline: no liveness probe response after %.0fs stale (guild=%s)",
            row.id,
            WORKER_OFFLINE_AFTER_SECONDS,
            row.guild_slug,
        )
    return len(workers_marked_offline) + len(stale_agent_only_rows)


async def _stale_worker_sweeper() -> None:
    """Background task: probe stale workers and mark unresponsive workers offline."""
    logger.info(
        "Stale-worker sweeper started: threshold=%.1fs interval=%.1fs",
        WORKER_OFFLINE_AFTER_SECONDS,
        WORKER_SWEEP_INTERVAL_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(WORKER_SWEEP_INTERVAL_SECONDS)
            await _sweep_stale_workers_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale-worker sweeper iteration failed")


# ---------------------------------------------------------------------------
# Lifespan + app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-apply our logging config after uvicorn has set up its own handlers so
    # the format remains consistent for the lifetime of the server.
    _log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    _log_format = os.environ.get("LOG_FORMAT", "colored")
    logging.config.dictConfig(_get_logging_config(_log_level, _log_format))
    await reset_connection_state()
    # Pre-warm the models.dev cache so the first /api/models request is fast.
    from util.models_dev import fetch_providers as _fetch_providers  # noqa: PLC0415

    asyncio.ensure_future(_fetch_providers())
    sweeper = spawn(_stale_worker_sweeper(), name="stale-worker-sweeper")
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except (asyncio.CancelledError, Exception):
            pass
        await _shutdown_webhook_debouncer()


class _AccessLogMiddleware(BaseHTTPMiddleware):
    """Log every HTTP request with method, path, status, response time, and client IP."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        xff = request.headers.get("x-forwarded-for")
        client_ip = (
            xff.split(",")[0].strip() if xff else (request.client.host if request.client else "-")
        )
        qs = f"?{request.url.query}" if request.url.query else ""
        logger.info(
            "%s %s%s → %d (%.1fms) %s",
            request.method,
            request.url.path,
            qs,
            response.status_code,
            elapsed_ms,
            client_ip,
        )
        return response


app = FastAPI(title="Pioneer Square", lifespan=lifespan)

app.add_middleware(_AccessLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Mount routers
# ---------------------------------------------------------------------------

from routes import agents as _agents_routes  # noqa: E402
from routes import auth as _auth_routes  # noqa: E402
from routes import debug as _debug_routes  # noqa: E402
from routes import foreman as _foreman_routes  # noqa: E402
from routes import guilds as _guilds_routes  # noqa: E402
from routes import models as _models_routes  # noqa: E402
from routes import push as _push_routes  # noqa: E402
from routes import tasks as _tasks_routes  # noqa: E402
from routes import usage as _usage_routes  # noqa: E402
from routes import webhooks as _webhooks_routes  # noqa: E402
from routes import websocket as _ws_routes  # noqa: E402
from routes import wellknown as _wellknown_routes  # noqa: E402
from routes import workers as _workers_routes  # noqa: E402
from routes.webhooks import shutdown_debouncer as _shutdown_webhook_debouncer  # noqa: E402

app.include_router(_wellknown_routes.router)
app.include_router(_auth_routes.router)
app.include_router(_guilds_routes.router)
app.include_router(_agents_routes.router)
app.include_router(_workers_routes.router)
app.include_router(_tasks_routes.router)
app.include_router(_usage_routes.router)
app.include_router(_foreman_routes.router)
app.include_router(_webhooks_routes.router)
app.include_router(_push_routes.router)
app.include_router(_ws_routes.router)
app.include_router(_debug_routes.router)
app.include_router(_models_routes.router)


# ---------------------------------------------------------------------------
# Backward-compatible re-exports for tests that ``from main import …`` private
# helpers directly. Don't use these in new code — import from the source module.
# ---------------------------------------------------------------------------

from routes.tasks import (  # noqa: E402,F401
    DEFAULT_FINALIZE_TTL,
    FinalizeBody,
    _resolve_finalize_deleted_at,
)
from utils import build_spawn_worker_env as _build_spawn_worker_env  # noqa: E402,F401
from utils import decode_claude_oauth_token as _decode_claude_oauth_token  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Static SPA assets. The frontend is built into ./static by the Dockerfile and
# served from the same origin so VITE_API_BASE='' and the WebSocket's
# window.location.host both resolve to this backend.
# ---------------------------------------------------------------------------


class _SPAStaticFiles(StaticFiles):
    """StaticFiles that falls back to index.html for unknown paths (SPA routing)."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and path != "index.html":
                return await super().get_response("index.html", scope)
            raise


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_VITE_DEV_URL = os.environ.get("VITE_DEV_URL", "").rstrip("/")

if _VITE_DEV_URL:
    # Dev mode: proxy all unmatched requests to the Vite dev server so GitHub
    # OAuth can redirect to the backend URL while Vite serves the SPA.
    # Takes precedence over static/ so it works in the container too.
    import httpx
    from starlette.background import BackgroundTask
    from starlette.responses import StreamingResponse

    # No timeout: this is a transparent dev proxy and Vite responses can be
    # slow or long-lived (HMR, SSE, large bundles). The default 5s httpx
    # timeout would otherwise raise httpx.ReadTimeout and crash the request.
    _vite_client = httpx.AsyncClient(base_url=_VITE_DEV_URL, follow_redirects=False, timeout=None)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "OPTIONS"])
    async def _vite_proxy(request: Request, path: str) -> Response:
        url = httpx.URL(path=f"/{path}", query=request.url.query.encode("utf-8"))
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection")
        }
        rp_req = _vite_client.build_request(request.method, url, headers=headers)
        rp_resp = await _vite_client.send(rp_req, stream=True)
        return StreamingResponse(
            rp_resp.aiter_raw(),
            status_code=rp_resp.status_code,
            headers=dict(rp_resp.headers),
            background=BackgroundTask(rp_resp.aclose),
        )

elif _STATIC_DIR.is_dir():
    app.mount("/", _SPAStaticFiles(directory=_STATIC_DIR, html=True), name="spa")


if __name__ == "__main__":
    import uvicorn

    _main_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    _main_log_format = os.environ.get("LOG_FORMAT", "colored")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        access_log=True,
        log_level=_main_log_level.lower(),
        log_config=_get_logging_config(_main_log_level, _main_log_format),
    )
