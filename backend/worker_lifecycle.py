"""Worker lifecycle: version-stale detection, idle reaping, and spawn bookkeeping.

Workers are disposable. Since the idle reaper shuts down every backend-spawned
worker after PIONEER_WORKER_IDLE_TIMEOUT (default 30 min) of inactivity, and the
foreman respawns workers on demand from the guild's spawn defaults
(``guild_spawn_defaults``, refreshed on every successful spawn), there is no
drain-and-replace choreography anymore. Nothing runs at backend startup to
shut workers down — a restart never disrupts an in-progress task. A version
bump is handled with a lightweight nudge plus the reaper:

1. signal_stale_worker_on_join() — when a worker (re)connects, the join
   handler tells version-stale backend-spawned workers to shut down. The
   worker finishes any in-progress task and exits on its own.
2. The idle reaper force-kills whatever is left: containers that died before
   reconnecting (offline zombies) and workers that never processed the signal.

get_current_version() prefers the PIONEER_VERSION env var, else
"<YYYYMMDD>-<sha>" from the current commit (committer date, never wall-clock —
versions are compared for exact equality, so a nondeterministic date would mark
every worker perpetually stale).
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
from models import Agent, Guild, GuildSpawnDefaults, Task, TaskLog, Worker, live_tasks_filter
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from ws_types import WorkerShutdownMsg

logger = logging.getLogger(__name__)

# How long to wait after an explicit shutdown_worker request before force-killing
# an unresponsive worker's container. Generous by default (30 min) because a busy
# worker may be mid-task; this is the window the foreman AI waits on every
# ordinary "stop this worker" request.
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

# Minimum time a guild must wait between successful worker spawns, to guard
# against accidental spam (e.g. a user mashing /worker-spawn) and the resource
# waste of repeatedly starting containers. Enforced by callers that check
# check_worker_spawn_cooldown() before invoking spawn_worker() — currently just
# the Discord /worker-spawn command.
WORKER_SPAWN_COOLDOWN = timedelta(
    seconds=float(os.environ.get("PIONEER_WORKER_SPAWN_COOLDOWN", "300"))
)

# Grace period before a version-stale worker is drained. A worker spawned by the
# *previous* backend during a rolling deploy is still cold-starting on Fargate
# (image pull + per-guild credential fetch) when the new backend takes over; its
# version legitimately differs but it is a healthy newborn, not a zombie. Without
# this, every worker that boots slower than the gap between deploys is killed the
# instant it connects and reaped 30 min later having done zero work. Genuinely
# old zombies are older than this and still get reaped by the idle reaper.
# ponytail: 5 min covers Fargate cold-start; bump via env if boots run longer.
STALE_DRAIN_MIN_AGE = timedelta(seconds=float(os.environ.get("PIONEER_STALE_DRAIN_MIN_AGE", "300")))

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


async def record_worker_spawn(
    db,
    worker_id: str,
    container_id: str,
    *,
    guild_pk: int | None = None,
    repos: list[str] | None = None,
    tools: list[str] | None = None,
    agent_count: int | None = None,
) -> None:
    """Persist container_id, spawned_version, and started_at after a successful worker spawn.

    When *guild_pk* and a non-empty *repos* list are given, also upserts the
    guild's ``guild_spawn_defaults`` row so it always reflects the last
    successful spawn — the foreman's spawn_worker tool falls back to that row
    when a call omits repos/tools/agent_count.
    """
    await db.exec(
        update(Worker)
        .where(col(Worker.id) == worker_id)
        .values(
            container_id=container_id,
            spawned_version=get_current_version(),
            started_at=datetime.now(UTC),
        )
    )
    if guild_pk is not None and repos:
        stmt = pg_insert(GuildSpawnDefaults).values(
            guild_id=guild_pk,
            repos=json.dumps(repos),
            tools=json.dumps(tools or []),
            agent_count=agent_count,
            updated_at=datetime.now(UTC),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["guild_id"],
            set_={
                "repos": stmt.excluded.repos,
                "tools": stmt.excluded.tools,
                "agent_count": stmt.excluded.agent_count,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await db.exec(stmt)
    await db.commit()


async def get_guild_spawn_defaults(db, guild_pk: int) -> GuildSpawnDefaults | None:
    """Return the guild's last-successful-spawn parameters, or None if never recorded."""
    return (
        await db.exec(
            select(GuildSpawnDefaults).where(col(GuildSpawnDefaults.guild_id) == guild_pk)
        )
    ).one_or_none()


async def set_guild_spawn_defaults(
    db,
    guild_pk: int,
    *,
    repos: list[str],
    tools: list[str],
    agent_count: int | None,
) -> None:
    """Explicitly upsert a guild's ``guild_spawn_defaults`` row.

    Unlike :func:`record_worker_spawn` (which only refreshes the row as a
    side-effect of a successful spawn, and only when ``repos`` is non-empty),
    this is the operator-facing write path: it always replaces the full row so
    an owner can curate the guild's spawn baseline directly, including clearing
    repos/tools. Callers are expected to have committed their own transaction
    boundary; this commits before returning.

    ``updated_at`` is deliberately left untouched when the row already exists:
    ``check_worker_spawn_cooldown`` reads it as the last *spawn* time, and an
    owner editing defaults is not a spawn — bumping it would start a spurious
    spawn cooldown. On first insert there is no prior spawn to protect, so it
    seeds ``now()`` (the column is NOT NULL).
    """
    stmt = pg_insert(GuildSpawnDefaults).values(
        guild_id=guild_pk,
        repos=json.dumps(repos),
        tools=json.dumps(tools),
        agent_count=agent_count,
        updated_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["guild_id"],
        set_={
            "repos": stmt.excluded.repos,
            "tools": stmt.excluded.tools,
            "agent_count": stmt.excluded.agent_count,
        },
    )
    await db.exec(stmt)
    await db.commit()


async def check_worker_spawn_cooldown(db, guild_pk: int) -> timedelta | None:
    """Return the remaining cooldown if *guild_pk* spawned a worker too recently, else None.

    Reads ``guild_spawn_defaults.updated_at``, which record_worker_spawn()
    refreshes on every successful spawn — so this reflects the guild's last
    successful spawn regardless of which caller (Discord, foreman AI, REST)
    triggered it.
    """
    defaults = await get_guild_spawn_defaults(db, guild_pk)
    if defaults is None:
        return None
    remaining = WORKER_SPAWN_COOLDOWN - (datetime.now(UTC) - defaults.updated_at)
    return remaining if remaining > timedelta(0) else None


async def signal_stale_worker_on_join(
    guild_slug: str,
    worker_id: str,
    spawned_version: str | None,
    started_at: datetime | None = None,
) -> bool:
    """Tell a version-stale backend-spawned worker to shut down as it (re)connects.

    Called from the WS join handler. Only workers with a recorded
    spawned_version are ever signalled: externally-started workers (compose,
    manual CLI) have no version stamp and belong to whoever started them.
    The worker finishes any in-progress task and exits; the foreman respawns
    on demand from the guild's spawn defaults, and the idle reaper force-kills
    the container if the worker never acts on the signal.

    A worker spawned within STALE_DRAIN_MIN_AGE is spared: during a rolling
    deploy it was spawned by the previous backend and is only now finishing its
    cold-start, so its version differs but it is a healthy newborn, not a zombie.

    Returns True when a shutdown signal was sent.
    """
    if not spawned_version:
        return False
    current = get_current_version()
    if current is None or spawned_version == current:
        return False
    if started_at is not None and datetime.now(UTC) - started_at < STALE_DRAIN_MIN_AGE:
        logger.info(
            "worker_lifecycle: worker %s joined with stale version %s (current %s) but was "
            "spawned %ds ago — sparing it (deploy overlap, not a zombie)",
            worker_id,
            spawned_version,
            current,
            int((datetime.now(UTC) - started_at).total_seconds()),
        )
        return False
    logger.info(
        "worker_lifecycle: worker %s joined with stale version %s (current %s) — "
        "sending graceful shutdown",
        worker_id,
        spawned_version,
        current,
    )
    await broadcast_msg(
        guild_slug,
        WorkerShutdownMsg(workerId=worker_id, reason="backend restarted: version mismatch"),
    )
    return True


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
    #
    # Only task-scoped rows count. Worker-level lines (task_id IS NULL) include
    # the idle puller's periodic "Pulled <repo>" heartbeat every pull_interval
    # (300s) — counting those would keep an otherwise-idle worker looking busy
    # forever, since the pull interval is shorter than the idle cutoff.
    last_log = (
        await db.exec(
            select(func.max(col(TaskLog.timestamp))).where(
                col(TaskLog.worker_id) == worker_id,
                col(TaskLog.task_id).isnot(None),
            )
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
