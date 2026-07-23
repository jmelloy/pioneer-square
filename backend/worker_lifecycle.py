"""Worker lifecycle: detect stale workers after a version change and cycle them.

A version bump means the running worker containers hold old code, so every stale
worker must be drained (soft-killed, allowed to finish its in-progress task, then
shut down) and replaced by a fresh container that picks up the new code. A stale
worker reconnecting after the restart is *expected* (the backend restarts first,
then the worker's reconnect loop brings its WebSocket back within seconds) — it is
never a reason to spare the worker from replacement.

Lifecycle steps on backend startup
───────────────────────────────────
1. get_current_version()         — PIONEER_VERSION env var, or "<YYYYMMDD>-<sha>"
   from the current commit (date is the deterministic committer date, never
   wall-clock/build time — versions are compared for exact equality, so a
   nondeterministic date would mark every worker perpetually stale).
2. drain_stale_workers_on_startup() — find workers whose spawned_version differs
   from current; record drain_requested_at (informational) and best-effort
   broadcast a shutdown. Returns the list of stale worker IDs.
3. reset_connection_state()      — called by main.py AFTER step 2, sets all
   workers offline in the DB.
4. reconcile_stale_workers(ids)  — spawned as a background task AFTER step 3.
   Per worker, two phases:
     • Reconnect: reset_connection_state() just marked everyone offline, so
       "offline" now means "not yet reconnected", not "drained". Wait up to
       PIONEER_WORKER_RECONNECT_GRACE for the worker to come back online. If it
       never does, its container died during the deploy — force-kill (best
       effort) and replace it immediately.
     • Drain: once it is back online, send the soft-kill and wait for it to
       finish its task and go offline (its ack), up to PIONEER_WORKER_DRAIN_TIMEOUT.
       If it never goes offline it is wedged — force-kill its container.
   Either way, once the worker is down a replacement with a brand-new worker ID
   and name (never the drained worker's identity) is spawned via
   spawn_replacement_workers(). Replacing only after the old worker is confirmed
   down means there is never a moment with two live workers for the same slot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
from datetime import UTC, datetime, timedelta

import worker_runtime
from database import AsyncSessionLocal
from events import broadcast_msg, emit_terminal_line
from models import Agent, Guild, Task, TaskLog, Worker, live_tasks_filter
from sqlalchemy import func, update
from sqlmodel import col, select
from ws_types import WorkerShutdownMsg

logger = logging.getLogger(__name__)

# How long to let a stale worker finish its in-progress task and exit gracefully
# after the soft-kill before force-killing its container. Generous by default (30
# min) because a busy worker may be mid-task when a deploy lands.
WORKER_DRAIN_TIMEOUT = float(os.environ.get("PIONEER_WORKER_DRAIN_TIMEOUT", "1800"))

# How long to wait for a stale worker to reconnect after a backend restart before
# treating it as already dead (its container died during the deploy) and replacing
# it without waiting for a drain. Workers normally reconnect within seconds.
WORKER_RECONNECT_GRACE = float(os.environ.get("PIONEER_WORKER_RECONNECT_GRACE", "120"))

# How long to wait after an explicit shutdown_worker request before force-killing
# an unresponsive worker's container. Separate from WORKER_DRAIN_TIMEOUT (which
# guards the startup version-mismatch drain) since this is the window the foreman
# AI waits on every ordinary "stop this worker" request.
SHUTDOWN_FORCE_KILL_TIMEOUT = float(os.environ.get("PIONEER_SHUTDOWN_TIMEOUT", "1800"))

# How long a backend-spawned worker may sit with no live work before the idle
# reaper shuts it down (and stops its container/ECS task). 0 disables reaping.
WORKER_IDLE_TIMEOUT = float(os.environ.get("PIONEER_WORKER_IDLE_TIMEOUT", "1800"))

# How often the idle reaper wakes up to look for idle spawned workers.
WORKER_IDLE_SWEEP_INTERVAL = float(os.environ.get("PIONEER_WORKER_IDLE_SWEEP_INTERVAL", "60"))

# Escalation window for the idle reaper's graceful shutdown. Much shorter than
# SHUTDOWN_FORCE_KILL_TIMEOUT because a reaped worker is by definition idle —
# there is no in-progress task to wait for.
IDLE_REAP_FORCE_KILL_TIMEOUT = float(os.environ.get("PIONEER_IDLE_REAP_KILL_TIMEOUT", "300"))

# Task states that hold a worker alive: queued or actively-running work.
# "awaiting-review" deliberately does NOT hold a worker alive — a PR can sit in
# review for days, and send_followup routes to any idle worker (which pulls the
# branch from GitHub), so keeping the original worker running would defeat the
# reaper entirely.
_ACTIVE_TASK_STATES = ("pending", "working")

# Agent states that mean the worker is actively doing something right now.
_BUSY_AGENT_STATES = ("working", "thinking", "busy")


def get_current_version() -> str | None:
    """Return the running backend version.

    Prefers the PIONEER_VERSION env var (set at image build time). Falls back to
    ``<YYYYMMDD>-<short-sha>`` from the current commit (e.g. ``20260709-1e1ae98``)
    so local dev environments also get a version string that's easy to date at a
    glance. Returns None if neither is available.

    The date is the commit's committer date — deterministic per commit — never
    the wall-clock/build time. Versions are compared for exact equality to decide
    staleness, so a nondeterministic date would make every worker perpetually
    version-mismatched and trigger endless recycling.
    """
    v = os.environ.get("PIONEER_VERSION")
    if v:
        return v.strip()
    try:
        version = subprocess.check_output(
            ["git", "show", "-s", "--format=%cd-%h", "--date=format:%Y%m%d", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return version or None
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
    """Spawn one fresh replacement worker for each drained stale worker.

    The replacement carries the same repos and tools but gets a brand-new worker
    ID and name — spawn_worker() mints a fresh ``w-…`` id and derives the display
    name from it, so we deliberately omit ``name`` here rather than reuse the
    drained worker's identity (its name embeds the now-dead worker id).
    """
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
                "tools": json.loads(worker.tools or "[]"),
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
    """Force-kill the containers/ECS tasks backing the given worker IDs, if any."""
    async with AsyncSessionLocal() as db:
        workers = (await db.exec(select(Worker).where(col(Worker.id).in_(worker_ids)))).all()

    for worker in workers:
        if worker.container_id:
            try:
                # worker_runtime dispatches on the handle itself (Docker container
                # id vs ECS task ARN), so handles recorded under either runtime
                # remain killable after a deployment-model change.
                await worker_runtime.stop_worker_container(
                    worker.container_id, reason=f"force-kill of worker {worker.id}"
                )
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
            # Externally-started worker (compose or manual CLI): cannot force-kill.
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
    start = asyncio.get_event_loop().time()
    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(min(poll_interval, timeout - elapsed))
        elapsed = asyncio.get_event_loop().time() - start
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

    # Re-check right before escalating: the worker may have gone offline in the
    # window between the last poll above and now, and we don't want to force-kill
    # a container that already shut down gracefully.
    async with AsyncSessionLocal() as db:
        state = (
            await db.exec(select(col(Worker.state)).where(col(Worker.id) == worker_id))
        ).one_or_none()
    if state is None or state == "offline":
        logger.info(
            "worker_lifecycle: worker %s shut down gracefully just before the force-kill "
            "escalation — skipping container kill",
            worker_id,
        )
        return

    logger.warning(
        "worker_lifecycle: worker %s did not shut down within %.0fs of the graceful "
        "shutdown signal — force-killing its container",
        worker_id,
        timeout,
    )
    await _force_kill_containers({worker_id})


async def _guild_slugs_for_workers(worker_ids: set[str]) -> dict[str, str]:
    """Map each worker id to its guild slug, for broadcasting shutdown messages."""
    if not worker_ids:
        return {}
    async with AsyncSessionLocal() as db:
        rows = (
            await db.exec(
                select(col(Worker.id), col(Guild.slug).label("guild_slug"))
                .join(Guild, col(Guild.id) == col(Worker.guild_id))
                .where(col(Worker.id).in_(worker_ids))
            )
        ).all()
    return {wid: slug for wid, slug in rows}


async def reconcile_stale_workers(stale_ids: list[str]) -> None:
    """Drain every stale worker and replace it with a fresh one.

    Spawned as a background task *after* reset_connection_state() has run. On a
    version bump every stale worker holds old code and must be cycled, so a
    reconnect is never a reason to skip replacement — it is exactly what we wait
    for so the soft-kill can be delivered. Replacing only after the old worker is
    confirmed offline means there is never a moment with two live workers for the
    same slot, which is the double-spawn the previous spare-on-reconnect logic was
    guarding against.

    Per worker, two phases (see module docstring):
      • "reconnect" — wait up to WORKER_RECONNECT_GRACE for the worker to come
        back online. reset_connection_state() just marked everyone offline, so
        "offline" here means "not yet reconnected", not "drained". A worker that
        never reconnects had its container die during the deploy; force-kill (best
        effort) and replace it.
      • "drain" — once online, (re)send the soft-kill and wait up to
        WORKER_DRAIN_TIMEOUT for the worker to finish its task and go offline. If
        it never does it is wedged; force-kill its container. Either way, replace
        it once it is down.
    """
    if not stale_ids:
        return

    poll_interval = 5.0
    guild_slugs = await _guild_slugs_for_workers(set(stale_ids))
    loop = asyncio.get_event_loop()
    # Per-worker phase and the monotonic time that phase began (for timeouts).
    phase = {wid: "reconnect" for wid in stale_ids}
    phase_started = {wid: loop.time() for wid in stale_ids}
    pending = set(stale_ids)

    async def _send_soft_kill(wid: str) -> None:
        slug = guild_slugs.get(wid)
        if not slug:
            return
        try:
            await broadcast_msg(
                slug,
                WorkerShutdownMsg(workerId=wid, reason="backend restarted: version mismatch"),
            )
        except Exception:
            logger.warning(
                "worker_lifecycle: could not send soft-kill to worker %s", wid, exc_info=True
            )

    while pending:
        await asyncio.sleep(poll_interval)
        now = loop.time()
        async with AsyncSessionLocal() as db:
            online = set(
                (
                    await db.exec(
                        select(col(Worker.id)).where(
                            col(Worker.id).in_(pending), col(Worker.state) != "offline"
                        )
                    )
                ).all()
            )

        drained: list[str] = []  # went offline after draining → replace
        dead: list[str] = []  # never reconnected / wedged → force-kill + replace
        to_signal: list[str] = []  # online & stale → (re)send soft-kill

        for wid in list(pending):
            if phase[wid] == "reconnect":
                if wid in online:
                    # Back online — begin draining and deliver the soft-kill.
                    phase[wid] = "drain"
                    phase_started[wid] = now
                    to_signal.append(wid)
                elif now - phase_started[wid] >= WORKER_RECONNECT_GRACE:
                    dead.append(wid)  # container died during the deploy
            else:  # draining
                if wid not in online:
                    drained.append(wid)  # ack: finished its task and exited
                elif now - phase_started[wid] >= WORKER_DRAIN_TIMEOUT:
                    dead.append(wid)  # wedged: force-kill below
                else:
                    to_signal.append(wid)  # still draining — re-send (idempotent)

        for wid in to_signal:
            await _send_soft_kill(wid)

        if dead:
            logger.warning(
                "worker_lifecycle: %d stale worker(s) did not drain in time — "
                "force-killing their containers: %s",
                len(dead),
                sorted(dead),
            )
            await _force_kill_containers(set(dead))

        # Replace every worker now confirmed down (drained cleanly or force-killed).
        for wid in (*drained, *dead):
            await spawn_replacement_workers([wid])
            logger.info("worker_lifecycle: stale worker %s cycled — replacement spawned", wid)
            pending.discard(wid)

    logger.info("worker_lifecycle: all stale workers drained and replaced")


# ---------------------------------------------------------------------------
# Idle-worker reaper — automatic shutdown of backend-spawned workers that have
# had nothing to do for WORKER_IDLE_TIMEOUT. Only workers with a recorded spawn
# handle (container_id) are ever reaped: externally-started workers (compose,
# manual CLI) belong to whoever started them.
# ---------------------------------------------------------------------------


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Attach UTC to naive datetimes read back from the DB (SQLite test runs)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _worker_is_inactive(db, worker_id: str, cutoff: datetime) -> bool:
    """True when *worker_id* has no live work and no activity since *cutoff*."""
    active_task = (
        await db.exec(
            select(col(Task.id))
            .where(
                col(Task.worker_id) == worker_id,
                col(Task.state).in_(_ACTIVE_TASK_STATES),
                live_tasks_filter(),
            )
            .limit(1)
        )
    ).first()
    if active_task is not None:
        return False

    busy_agent = (
        await db.exec(
            select(col(Agent.id))
            .where(
                col(Agent.worker_id) == worker_id,
                col(Agent.state).in_(_BUSY_AGENT_STATES),
            )
            .limit(1)
        )
    ).first()
    if busy_agent is not None:
        return False

    # Last activity = the newest task-log line attributed to this worker.
    # TaskLog rows are written throughout a run, so the newest one marks the
    # end of the worker's most recent piece of work; a worker that never ran
    # anything has no rows and falls back to its spawn time (already checked
    # against the cutoff by the candidate query).
    last_log = (
        await db.exec(
            select(func.max(col(TaskLog.timestamp))).where(col(TaskLog.worker_id) == worker_id)
        )
    ).one_or_none()
    last_log = _ensure_utc(last_log)
    return last_log is None or last_log < cutoff


async def reap_idle_workers_once(now: datetime | None = None) -> list[str]:
    """One idle-reap pass; returns the IDs of the workers that were shut down.

    Two kinds of spawned workers are reaped once they pass the idle cutoff:

    - **Online and idle** — no pending/working task, no busy agent, and no
      task-log activity within the window. Gets the graceful shutdown signal
      (same path as shutdown_worker) plus a short force-kill backstop.
    - **Offline zombies** — spawned but never (re)connected, or long dead. The
      container/ECS task is force-killed directly (a no-op if it already
      exited). Gated on ``last_seen`` also being past the cutoff so workers
      that are merely reconnecting after a backend restart are left alone.

    Both kinds are marked ``disabled`` so they are neither respawned on the
    next backend restart nor re-examined on the next sweep.
    """
    if WORKER_IDLE_TIMEOUT <= 0:
        return []
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=WORKER_IDLE_TIMEOUT)

    async with AsyncSessionLocal() as db:
        candidates = (
            await db.exec(
                select(
                    col(Worker.id),
                    col(Worker.state),
                    col(Worker.last_seen),
                    col(Guild.slug).label("guild_slug"),
                )
                .join(Guild, col(Guild.id) == col(Worker.guild_id))
                .where(
                    col(Worker.container_id).isnot(None),
                    col(Worker.disabled).is_(False),
                    col(Worker.started_at).isnot(None),
                    col(Worker.started_at) < cutoff,
                )
            )
        ).all()

    reaped: list[str] = []
    for worker_id, state, last_seen, guild_slug in candidates:
        try:
            if state == "offline":
                # Never-connected zombie or already-dead container. Skip if it
                # was alive recently — it may be mid-reconnect after a restart.
                last_seen = _ensure_utc(last_seen)
                if last_seen is not None and last_seen >= cutoff:
                    continue
                async with AsyncSessionLocal() as db:
                    await db.exec(
                        update(Worker)
                        .where(col(Worker.id) == worker_id)
                        .values(disabled=True, drain_requested_at=now)
                    )
                    await db.commit()
                logger.info(
                    "worker_lifecycle: reaping offline spawned worker %s "
                    "(no connection since spawn or last activity > %.0fs ago)",
                    worker_id,
                    WORKER_IDLE_TIMEOUT,
                )
                await _force_kill_containers({worker_id})
                reaped.append(worker_id)
                continue

            async with AsyncSessionLocal() as db:
                inactive = await _worker_is_inactive(db, worker_id, cutoff)
                if not inactive:
                    continue
                await db.exec(
                    update(Worker)
                    .where(col(Worker.id) == worker_id)
                    .values(disabled=True, drain_requested_at=now)
                )
                await db.commit()

            reason = (
                f"idle for over {int(WORKER_IDLE_TIMEOUT)}s — automatic shutdown of spawned worker"
            )
            logger.info("worker_lifecycle: reaping idle worker %s (%s)", worker_id, reason)
            await broadcast_msg(guild_slug, WorkerShutdownMsg(workerId=worker_id, reason=reason))
            await emit_terminal_line(guild_slug, worker_id, f"[lifecycle] {reason}")

            # Backstop: the worker is idle, so it should exit within seconds of
            # the signal; force-kill its container/ECS task if it doesn't.
            from util.tasks import spawn  # noqa: PLC0415

            spawn(
                force_kill_worker_if_unresponsive(worker_id, timeout=IDLE_REAP_FORCE_KILL_TIMEOUT),
                name=f"idle-reap-escalate:{worker_id}",
            )
            reaped.append(worker_id)
        except Exception:
            logger.warning(
                "worker_lifecycle: idle-reap failed for worker %s", worker_id, exc_info=True
            )
    return reaped


async def idle_worker_reaper() -> None:
    """Background task: periodically reap spawned workers idle past the timeout."""
    if WORKER_IDLE_TIMEOUT <= 0:
        logger.info("worker_lifecycle: idle-worker reaper disabled (PIONEER_WORKER_IDLE_TIMEOUT=0)")
        return
    logger.info(
        "worker_lifecycle: idle-worker reaper started: timeout=%.0fs interval=%.0fs",
        WORKER_IDLE_TIMEOUT,
        WORKER_IDLE_SWEEP_INTERVAL,
    )
    while True:
        try:
            await asyncio.sleep(WORKER_IDLE_SWEEP_INTERVAL)
            await reap_idle_workers_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("worker_lifecycle: idle-worker reaper iteration failed")
