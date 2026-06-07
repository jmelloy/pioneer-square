"""Worker lifecycle: detect stale workers after a version change and drain/kill them.

Lifecycle steps on backend startup
───────────────────────────────────
1. get_current_version()         — PIONEER_VERSION env var or git short SHA
2. drain_stale_workers_on_startup() — find workers whose spawned_version differs
   from current; send graceful WS shutdown signal; record drain_requested_at.
   Returns list of stale worker IDs.
3. reset_connection_state()      — called by main.py AFTER step 2, sets all
   workers offline in the DB.
4. force_kill_stale_workers(ids) — spawned as a background task AFTER step 3;
   waits PIONEER_WORKER_DRAIN_TIMEOUT seconds then force-kills surviving
   containers via Docker SDK using the stored container_id.
   (Skipped when container_id is NULL — non-Docker workers rely on step 2 only.)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import UTC, datetime

from database import AsyncSessionLocal
from events import broadcast_msg
from models import Guild, Worker
from sqlalchemy import update
from sqlmodel import col, select
from ws_types import WorkerShutdownMsg

logger = logging.getLogger(__name__)

# How long to wait for stale workers to exit gracefully before force-killing.
WORKER_DRAIN_TIMEOUT = float(os.environ.get("PIONEER_WORKER_DRAIN_TIMEOUT", "60"))


def get_current_version() -> str | None:
    """Return the running backend version.

    Prefers the PIONEER_VERSION env var (set at image build time).  Falls back
    to the short git SHA so local dev environments also get a version string.
    Returns None if neither is available.
    """
    v = os.environ.get("PIONEER_VERSION")
    if v:
        return v.strip()
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return sha or None
    except Exception:
        return None


async def drain_stale_workers_on_startup() -> list[str]:
    """Detect workers spawned by a previous backend version and send shutdown signals.

    Must be awaited directly *before* reset_connection_state() so the DB still
    holds the non-offline state that identifies which workers were running before
    this restart.

    Returns the list of stale worker IDs so the caller can pass them to
    force_kill_stale_workers() as a background task after reset_connection_state().
    """
    current_version = get_current_version()
    if current_version is None:
        logger.warning(
            "worker_lifecycle: cannot determine backend version; skipping stale-worker drain"
        )
        return []

    # Step 1: find workers whose recorded version differs from the current deploy.
    async with AsyncSessionLocal() as db:
        rows = (
            await db.exec(
                select(Worker, col(Guild.guild_id).label("guild_slug"))
                .join(Guild, col(Guild.id) == col(Worker.guild_id))
                .where(
                    col(Worker.spawned_version).isnot(None),
                    col(Worker.spawned_version) != current_version,
                    col(Worker.state) != "offline",
                )
            )
        ).all()

    if not rows:
        logger.info(
            "worker_lifecycle: no stale workers found (current version=%s)", current_version
        )
        return []

    logger.info(
        "worker_lifecycle: %d stale worker(s) from a previous deploy — "
        "sending shutdown signals, force-kill in %.0fs (current version=%s)",
        len(rows),
        WORKER_DRAIN_TIMEOUT,
        current_version,
    )

    # Step 2: send graceful-shutdown WS signal and record drain timestamp.
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        for worker, guild_slug in rows:
            try:
                # Best-effort: no active connections on a cold restart, but
                # this reaches workers during a hot-reload restart.
                await broadcast_msg(
                    guild_slug,
                    WorkerShutdownMsg(
                        workerId=worker.id,
                        reason="backend restarted: version mismatch",
                    ),
                )
            except Exception:
                logger.warning(
                    "worker_lifecycle: could not send shutdown signal to worker %s",
                    worker.id,
                    exc_info=True,
                )
            await db.exec(
                update(Worker)
                .where(col(Worker.id) == worker.id)
                .values(drain_requested_at=now)
            )
        await db.commit()

    return [worker.id for worker, _ in rows]


async def force_kill_stale_workers(stale_ids: list[str]) -> None:
    """Wait the drain window then force-kill surviving stale containers.

    Must be spawned as a background task *after* reset_connection_state() has
    run.  Because reset_connection_state() marks all workers offline in the DB,
    we identify survivors by container_id rather than by DB state.
    """
    if not stale_ids:
        return

    await asyncio.sleep(WORKER_DRAIN_TIMEOUT)

    # Step 3: fetch stale workers that have a container_id to kill.
    async with AsyncSessionLocal() as db:
        workers = (
            await db.exec(select(Worker).where(col(Worker.id).in_(stale_ids)))
        ).all()

    for worker in workers:
        if worker.container_id:
            # Step 4: force-kill the Docker container.
            try:
                import docker  # noqa: PLC0415

                # Use asyncio.to_thread for all blocking Docker SDK calls.
                client = await asyncio.to_thread(docker.from_env)
                container = await asyncio.to_thread(client.containers.get, worker.container_id)
                await asyncio.to_thread(container.kill)
                logger.info(
                    "worker_lifecycle: force-killed container %s (worker %s)",
                    worker.container_id[:12],
                    worker.id,
                )
            except Exception:
                logger.warning(
                    "worker_lifecycle: failed to force-kill container for worker %s",
                    worker.id,
                    exc_info=True,
                )
        else:
            # Non-Docker worker (started via compose or manual CLI): cannot force-kill.
            logger.warning(
                "worker_lifecycle: stale worker %s has no container_id; "
                "relying on graceful-shutdown signal only",
                worker.id,
            )
