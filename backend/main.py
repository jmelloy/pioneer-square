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

from database import AsyncSessionLocal
from events import agent_owners, broadcast
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from lock_service import LockService
from models import Agent, Guild, Lock, Task, Worker
from sqlalchemy import literal, select, update
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from util.tasks import spawn

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

# How long an agent/worker can go without sending any WebSocket message before
# the sweeper marks it offline. Workers send an application-level `ping` every
# ~25s, so 90s = three missed heartbeats.
WORKER_OFFLINE_AFTER_SECONDS = float(os.environ.get("WORKER_OFFLINE_AFTER_SECONDS", "90"))
# How often the sweeper task wakes up to look for stale agents.
WORKER_SWEEP_INTERVAL_SECONDS = float(os.environ.get("WORKER_SWEEP_INTERVAL_SECONDS", "30"))


async def reset_connection_state() -> None:
    # On every startup, no worker processes are connected yet.
    async with AsyncSessionLocal() as db:
        await db.execute(update(Worker).values(state="offline"))
        await db.execute(
            update(Agent)
            .where(Agent.worker_id.in_(select(Worker.id).where(Worker.state == "offline")))
            .values(state="offline", activity=None, current_task_id=None)
        )
        await db.commit()


async def _sweep_stale_workers_once() -> int:
    """One pass of the stale-worker sweep. Returns the total number of agents
    and zombie workers marked offline. Extracted for direct testing."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=WORKER_OFFLINE_AFTER_SECONDS)).isoformat()
    stale_agents: list = []
    zombie_workers: list = []
    agent_cascade_wids: set[str] = set()
    async with AsyncSessionLocal() as db:
        # Zombie-worker pass runs first so the agent-state guard sees pre-update
        # states.  A zombie is a worker stuck "online" with a stale last_seen
        # whose agents are ALL already offline (the WS close handler marked them
        # offline but failed to flip the Worker row due to the agent_id/worker_id
        # bug).  Workers that still have any non-offline agent are skipped here
        # and handled via the agent cascade below.
        zombie_workers = (
            await db.execute(
                select(Worker.id, Worker.guild_id, Guild.guild_id.label("guild_slug"))
                .join(Guild, Guild.id == Worker.guild_id)
                .where(Worker.state != "offline")
                .where(Worker.last_seen.isnot(None))
                .where(Worker.last_seen < cutoff)
                .where(
                    ~select(Agent.id)
                    .where(Agent.worker_id == Worker.id)
                    .where(Agent.state != "offline")
                    .exists()
                )
            )
        ).all()

        stale_agents = (
            await db.execute(
                select(
                    Agent.id, Agent.guild_id, Agent.worker_id, Guild.guild_id.label("guild_slug")
                )
                .join(Guild, Guild.id == Agent.guild_id)
                .where(Agent.state != "offline")
                .where(Agent.last_seen.isnot(None))
                .where(Agent.last_seen < cutoff)
            )
        ).all()
        # stale_worker_keys stores (worker_id, guild_id) where guild_id is the
        # integer FK to guilds.id (not the text slug guild_slug selected above).
        stale_worker_keys: set[tuple[str, int]] = set()
        for row in stale_agents:
            await db.execute(
                update(Agent)
                .where(Agent.id == row.id, Agent.guild_id == row.guild_id)
                .values(state="offline", activity=None, current_task_id=None)
            )
            if row.worker_id:
                stale_worker_keys.add((row.worker_id, row.guild_id))
                agent_cascade_wids.add(row.worker_id)
            agent_owners.pop(row.id, None)

        for row in zombie_workers:
            stale_worker_keys.add((row.id, row.guild_id))

        for worker_id, gpk in stale_worker_keys:
            await db.execute(
                update(Worker)
                .where(Worker.id == worker_id, Worker.guild_id == gpk)
                .values(state="offline")
            )
        if stale_agents or zombie_workers:
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
                select(Task.id).where(
                    Task.state == "working",
                    # Lock exists for this task and has been held long enough
                    # that we can assume it's not a brand-new dispatch.
                    # Use the || operator for string concat — SQLite does not
                    # have a concat() function; this is dialect-neutral for
                    # single-DB deployments and avoids func.concat().
                    select(Lock.key)
                    .where(
                        Lock.key == literal("task:").op("||")(Task.id),
                        Lock.acquired_at < cutoff_lock,
                    )
                    .exists(),
                    # No agent is actively running this task right now.
                    ~select(Agent.id)
                    .where(
                        Agent.current_task_id == Task.id,
                        Agent.state.in_(("working", "thinking", "busy")),
                    )
                    .exists(),
                )
            )
        ).all()
        if orphaned:
            stale_task_ids = list(orphaned)
            for task_id in stale_task_ids:
                await db.execute(
                    update(Task)
                    .where(Task.id == task_id, Task.state == "working")
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

    for row in stale_agents:
        logger.warning(
            "Marking %s offline: no ping in over %.0fs (guild=%s)",
            row.id,
            WORKER_OFFLINE_AFTER_SECONDS,
            row.guild_slug,
        )
        await broadcast(
            row.guild_slug,
            {"type": "agent-state", "agentId": row.id, "state": "offline"},
        )
    # Log zombie workers not already covered by the per-agent log above.
    for row in zombie_workers:
        if row.id not in agent_cascade_wids:
            logger.warning(
                "Marking zombie worker %s offline: no ping in over %.0fs,"
                " no active agents (guild=%s)",
                row.id,
                WORKER_OFFLINE_AFTER_SECONDS,
                row.guild_slug,
            )
    return len(stale_agents) + len(zombie_workers)


async def _stale_worker_sweeper() -> None:
    """Background task: mark workers/agents offline if they haven't pinged recently.

    Runs forever (cancelled at app shutdown). The websocket library's own
    ping/pong catches dead TCP connections, but a worker process could be
    stuck in a way that still answers protocol pings; the application-level
    `ping` heartbeat is what this sweeper actually relies on.
    """
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
from routes import foreman as _foreman_routes  # noqa: E402
from routes import guilds as _guilds_routes  # noqa: E402
from routes import tasks as _tasks_routes  # noqa: E402
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
app.include_router(_foreman_routes.router)
app.include_router(_webhooks_routes.router)
app.include_router(_ws_routes.router)


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

    _vite_client = httpx.AsyncClient(base_url=_VITE_DEV_URL, follow_redirects=False)

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
