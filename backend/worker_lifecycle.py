"""Worker lifecycle: detect stale workers after a version change and drain/kill them.

Lifecycle steps on backend startup
───────────────────────────────────
1. get_current_version()         — PIONEER_VERSION env var or git short SHA
2. drain_stale_workers_on_startup() — find workers whose spawned_version differs
   from current; send graceful WS shutdown signal; record drain_requested_at.
   Returns list of stale worker IDs.
3. reset_connection_state()      — called by main.py AFTER step 2, sets all
   workers offline in the DB.
4. reconcile_stale_workers(ids)  — spawned as a background task AFTER step 3;
   waits up to PIONEER_WORKER_DRAIN_TIMEOUT seconds, dropping any worker that
   reconnects on its own from the replacement list (a cold restart drops every
   WebSocket at once, so the shutdown broadcast in step 2 reaches no one — the
   worker's own reconnect loop brings it back within seconds and it should
   count against the desired worker total, not trigger a redundant spawn).
   Workers that never come back within the window get their container
   force-killed (via Docker SDK, using the stored container_id — skipped when
   NULL) and a single replacement spawned via spawn_replacement_workers().
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
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

# How long to wait after an explicit shutdown_worker request before force-killing
# an unresponsive worker's container. Separate from WORKER_DRAIN_TIMEOUT (which
# guards the much rarer startup version-mismatch drain) since this is the window
# the foreman AI waits on every ordinary "stop this worker" request.
SHUTDOWN_FORCE_KILL_TIMEOUT = float(os.environ.get("PIONEER_SHUTDOWN_TIMEOUT", "30"))


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


async def generate_worker_id(db) -> str:
    """Return a fresh ``w-``-prefixed worker ID guaranteed not to collide with an existing row.

    secrets.token_hex(3) draws from ~16M possible suffixes, so a collision is
    unlikely but not impossible — and an unchecked collision would silently
    reassign an existing worker's identity to a new registration. Retry a
    handful of times against the DB before giving up.
    """
    for _ in range(5):
        candidate = "w-" + secrets.token_hex(3)
        existing = (
            await db.exec(select(col(Worker.id)).where(col(Worker.id) == candidate))
        ).one_or_none()
        if existing is None:
            return candidate
    raise RuntimeError("could not generate a unique worker_id after 5 attempts")


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


async def _force_kill_containers(worker_ids: set[str]) -> None:
    """Force-kill the Docker containers backing the given worker IDs, if any."""
    async with AsyncSessionLocal() as db:
        workers = (await db.exec(select(Worker).where(col(Worker.id).in_(worker_ids)))).all()

    # Create the Docker client once; avoids per-container connection overhead and
    # ensures consistent failure behaviour across all kills in this batch.
    docker_client = None
    for worker in workers:
        if worker.container_id:
            try:
                import docker  # noqa: PLC0415

                # Use asyncio.to_thread for all blocking Docker SDK calls.
                if docker_client is None:
                    docker_client = await asyncio.to_thread(docker.from_env)
                container = await asyncio.to_thread(
                    docker_client.containers.get, worker.container_id
                )
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


async def force_kill_worker_if_unresponsive(
    worker_id: str, timeout: float = SHUTDOWN_FORCE_KILL_TIMEOUT
) -> None:
    """Give a worker time to shut down gracefully; force-kill its container if it doesn't.

    Spawned as a background task right after shutdown_worker broadcasts a graceful
    WorkerShutdownMsg, so an in-progress task on that worker gets a chance to finish
    before anything is force-killed. Only escalates to a hard Docker kill (last
    resort) if the worker never reports going offline within *timeout* seconds —
    e.g. because it's wedged and never received/processed the shutdown signal.
    """
    poll_interval = 5.0
    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(min(poll_interval, timeout - elapsed))
        elapsed += poll_interval
        async with AsyncSessionLocal() as db:
            state = (
                await db.exec(select(col(Worker.state)).where(col(Worker.id) == worker_id))
            ).one_or_none()
        if state is None or state == "offline":
            logger.info(
                "worker_lifecycle: worker %s shut down gracefully within %.0fs",
                worker_id,
                elapsed,
            )
            return

    logger.warning(
        "worker_lifecycle: worker %s did not shut down within %.0fs of the graceful "
        "shutdown signal — force-killing its container",
        worker_id,
        timeout,
    )
    await _force_kill_containers({worker_id})


async def reconcile_stale_workers(stale_ids: list[str]) -> None:
    """Give drained workers a chance to reconnect on their own before replacing them.

    Must be spawned as a background task *after* reset_connection_state() has
    run.  Spawning a replacement immediately for every drained worker (the old
    behaviour) overshoots the configured worker count on an ordinary restart:
    the worker process's own reconnect loop brings it back within seconds —
    it never saw the graceful-shutdown broadcast in the first place, since a
    cold restart drops every WebSocket before that message can be sent — so
    counting it as gone and spawning a full replacement leaves two workers
    running under two different worker_ids. Waiting for the drain window and
    checking who has already reconnected keeps replacement spawns limited to
    workers that are actually still missing.
    """
    if not stale_ids:
        return

    # Poll every 5 s, dropping any worker that reconnects from the pending set.
    poll_interval = 5.0
    elapsed = 0.0
    pending = set(stale_ids)
    while pending and elapsed < WORKER_DRAIN_TIMEOUT:
        await asyncio.sleep(min(poll_interval, WORKER_DRAIN_TIMEOUT - elapsed))
        elapsed += poll_interval
        async with AsyncSessionLocal() as db:
            reconnected = set(
                (
                    await db.exec(
                        select(col(Worker.id)).where(
                            col(Worker.id).in_(pending), col(Worker.state) != "offline"
                        )
                    )
                ).all()
            )
        if reconnected:
            logger.info(
                "worker_lifecycle: %d stale worker(s) reconnected on their own — "
                "no replacement needed: %s",
                len(reconnected),
                sorted(reconnected),
            )
            pending -= reconnected

    if not pending:
        logger.info("worker_lifecycle: all stale workers reconnected within the drain window")
        return

    logger.info(
        "worker_lifecycle: %d stale worker(s) did not reconnect within %.0fs — "
        "force-killing and spawning replacements: %s",
        len(pending),
        WORKER_DRAIN_TIMEOUT,
        sorted(pending),
    )
    await _force_kill_containers(pending)
    await spawn_replacement_workers(list(pending))
