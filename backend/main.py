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
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from database import AsyncSessionLocal
from events import agent_owners, broadcast
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from models import Agent, Worker
from sqlalchemy import select, update
from starlette.exceptions import HTTPException as StarletteHTTPException
from util.tasks import spawn

# Load .env (looked up from CWD upward, then alongside this file) before any
# code reads os.environ, so ANTHROPIC_API_KEY etc. are available.
try:
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

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


async def init_db() -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    def run_migrations():
        cfg = Config(Path(__file__).resolve().parent / "alembic.ini")
        db_url = os.environ.get(
            "DATABASE_URL", f"sqlite+aiosqlite:///{os.environ.get('DB_PATH', 'pioneer_square.db')}"
        )
        # Need a sync engine for inspection; strip aiosqlite async driver prefix.
        sync_url = db_url.replace("+aiosqlite", "")
        engine = create_engine(sync_url)
        try:
            tables = inspect(engine).get_table_names()
            has_alembic = "alembic_version" in tables
            has_data = "guilds" in tables
            # Pre-Alembic database: stamp to current head so upgrade is a no-op.
            if has_data and not has_alembic:
                command.stamp(cfg, "head")
        finally:
            engine.dispose()
        command.upgrade(cfg, "head")

    await asyncio.to_thread(run_migrations)

    # On every startup, no worker processes are connected yet.
    async with AsyncSessionLocal() as db:
        await db.execute(update(Worker).values(state="offline"))
        await db.execute(
            update(Agent)
            .where(Agent.worker_id.in_(select(Worker.id).where(Worker.state == "offline")))
            .values(state="offline")
        )
        await db.commit()


async def _sweep_stale_workers_once() -> int:
    """One pass of the stale-worker sweep. Returns the number of agents
    marked offline. Extracted for direct testing."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=WORKER_OFFLINE_AFTER_SECONDS)).isoformat()
    async with AsyncSessionLocal() as db:
        stale_agents = (
            await db.execute(
                select(Agent.id, Agent.guild_id, Agent.worker_id)
                .where(Agent.state != "offline")
                .where(Agent.last_seen.isnot(None))
                .where(Agent.last_seen < cutoff)
            )
        ).all()
        if not stale_agents:
            return 0
        stale_worker_keys: set[tuple[str, str]] = set()
        for row in stale_agents:
            await db.execute(
                update(Agent)
                .where(Agent.id == row.id, Agent.guild_id == row.guild_id)
                .values(state="offline", activity=None)
            )
            if row.worker_id:
                stale_worker_keys.add((row.worker_id, row.guild_id))
            agent_owners.pop(row.id, None)
        for worker_id, gid in stale_worker_keys:
            await db.execute(
                update(Worker)
                .where(Worker.id == worker_id, Worker.guild_id == gid)
                .values(state="offline")
            )
        await db.commit()
    for row in stale_agents:
        logger.warning(
            "Marking %s offline: no ping in over %.0fs (guild=%s)",
            row.id,
            WORKER_OFFLINE_AFTER_SECONDS,
            row.guild_id,
        )
        await broadcast(
            row.guild_id,
            {"type": "agent-state", "agentId": row.id, "state": "offline"},
        )
    return len(stale_agents)


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
    await init_db()
    # Uvicorn sets the root logger to WARNING, so foreman.* logs would be silently
    # dropped without an explicit handler.  Wire up a StreamHandler on the foreman
    # package logger so debug output reaches the console regardless of root level.
    foreman_log = logging.getLogger("foreman")
    foreman_log.setLevel(logging.DEBUG)
    if not foreman_log.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        foreman_log.addHandler(_h)
    foreman_log.propagate = False
    foreman_log.info("foreman logger active (level=DEBUG)")
    sweeper = spawn(_stale_worker_sweeper(), name="stale-worker-sweeper")
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="Pioneer Square", lifespan=lifespan)

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
from routes import workers as _workers_routes  # noqa: E402

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
if _STATIC_DIR.is_dir():
    app.mount("/", _SPAStaticFiles(directory=_STATIC_DIR, html=True), name="spa")
