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

import json
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


async def record_worker_spawn(db, worker_id: str, container_id: str) -> None:
    """Persist container_id, spawned_version, and started_at after a successful worker spawn."""
    await db.exec(
        update(Worker)
        .where(col(Worker.id) == worker_id)
        .values(
            container_id=container_id,
            spawned_version=get_current_version(),
            started_at=datetime.now(UTC),
        )
    )
    await db.commit()


async def spawn_replacement_workers(stale_ids: list[str]) -> None:
    """Spawn one fresh replacement worker for each drained stale worker."""
    if not stale_ids:
        return

    from foreman.tools import spawn_worker as _spawn_worker  # noqa: PLC0415

    async with AsyncSessionLocal() as db:
        rows = (
            await db.exec(
                select(Worker, col(Guild.slug).label("guild_slug"))
                .join(Guild, col(Guild.id) == col(Worker.guild_id))
                .where(col(Worker.id).in_(stale_ids), col(Worker.disabled).is_(False))
            )
        ).all()

    for worker, guild_slug in rows:
        try:
            inp = {
                "repos": json.loads(worker.repos or "[]"),
                "name": worker.name,
            }
            async with AsyncSessionLocal() as db:
                result_text, is_error = await _spawn_worker(
                    inp=inp,
                    guild_id=guild_slug,
                    guild_pk=worker.guild_id,
                    db=db,
                )
            if is_error:
                logger.warning(
                    "worker_lifecycle: failed to respawn replacement for guild %s: %s",
                    guild_slug,
                    result_text,
                )
            else:
                logger.info("worker_lifecycle: spawned replacement worker for guild %s", guild_slug)
        except Exception:
            logger.warning(
                "worker_lifecycle: exception respawning worker for guild %s",
                guild_slug,
                exc_info=True,
            )


async def drain_stale_workers_on_startup() -> list[str]:
    """Detect workers spawned by a previous backend version and send shutdown signals.

    Must be awaited directly *before* reset_connection_state() so the DB still
    holds the non-offline state that identifies which workers were running before
    this restart.

    Returns the list of stale worker IDs so the caller can pass them to
    force_kill_stale_workers() as a background task after reset_connection_state().
    """
    current_version = get_current_version()

    # Step 1: find stale workers.  When the version is undetermined we cannot
    # safely compare, so we conservatively treat *all* online workers as stale
    # rather than silently leaving potential zombies from a previous deploy.
    async with AsyncSessionLocal() as db:
        if current_version is None:
            logger.warning(
                "worker_lifecycle: cannot determine backend version; "
                "treating all online workers as stale to avoid leaving zombies running"
            )
            rows = (
                await db.exec(
                    select(Worker, col(Guild.slug).label("guild_slug"))
                    .join(Guild, col(Guild.id) == col(Worker.guild_id))
                    .where(col(Worker.state) != "offline", col(Worker.disabled).is_(False))
                )
            ).all()
        else:
            # Drain workers whose version differs from the current one, including workers
            # with NULL spawned_version (pre-migration rows from before version tracking
            # was introduced). NULL means we cannot confirm they match the current version,
            # so we conservatively treat them as stale.
            rows = (
                await db.exec(
                    select(Worker, col(Guild.slug).label("guild_slug"))
                    .join(Guild, col(Guild.id) == col(Worker.guild_id))
                    .where(
                        col(Worker.spawned_version) != current_version,
                        col(Worker.state) != "offline",
                        col(Worker.disabled).is_(False),
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
                update(Worker).where(col(Worker.id) == worker.id).values(drain_requested_at=now)
            )
        await db.commit()

    return [worker.id for worker, _ in rows]
