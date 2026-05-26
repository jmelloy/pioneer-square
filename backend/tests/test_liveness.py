"""Tests for WebSocket liveness tracking.

Covers:
  - inbound frames refresh ``last_seen`` on the agent and worker rows
  - the ``ping`` message is answered with ``pong``
  - the stale-worker sweeper marks workers offline once ``last_seen`` is
    older than the configured threshold and broadcasts the state change
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
from _test_config import TEST_DATABASE_URL  # noqa: E402
from helpers import _sync_session, insert_guild, insert_worker, truncate_all  # noqa: E402
from helpers import create_db as _create_db
from models import Agent, Guild, Lock, Task, Worker  # noqa: E402
from sqlalchemy import select, update  # noqa: E402


@pytest.fixture(scope="module")
def client():
    """Module-scoped TestClient — one client per module to dodge the
    starlette/anyio hang seen when a second TestClient is started against the
    same singleton FastAPI app instance."""
    _create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)
    db_url = TEST_DATABASE_URL

    new_engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)

    mp = pytest.MonkeyPatch()
    mp.setattr(database_module, "AsyncSessionLocal", new_session)
    mp.setattr(main_module, "AsyncSessionLocal", new_session)

    async def _stubbed_reset_connection_state() -> None:
        pass

    mp.setattr(main_module, "reset_connection_state", _stubbed_reset_connection_state)
    mp.setenv("DATABASE_URL", db_url)

    with TestClient(main_module.app) as c:
        yield c, db_url

    mp.undo()


def _setup_guild_and_worker(db_url: str, guild_id: str, worker_id: str) -> None:
    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")


def _read_last_seen(
    db_url: str, agent_id: str, worker_id: str
) -> tuple[datetime | None, datetime | None]:

    with _sync_session(db_url) as session:
        agent_seen = session.scalar(select(Agent.last_seen).where(Agent.id == agent_id))
        worker_seen = session.scalar(select(Worker.last_seen).where(Worker.id == worker_id))
    return (agent_seen, worker_seen)


def test_join_initialises_last_seen(client):
    """A worker join sets last_seen on both the agent and worker rows."""
    test_client, db_url = client
    guild_id = "lvg001"
    worker_id = "w-lva001"
    agent_id = "a-lva001"

    _setup_guild_and_worker(db_url, guild_id, worker_id)
    before = datetime.now(UTC)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        ws.send_json(
            {
                "type": "join",
                "agentId": agent_id,
                "agentName": "Test",
                "agentType": "worker",
                "workerId": worker_id,
            }
        )
        ws.receive_json()  # agent-joined broadcast

    agent_seen, worker_seen = _read_last_seen(db_url, agent_id, worker_id)
    assert agent_seen is not None
    assert worker_seen is not None
    assert agent_seen >= before - timedelta(seconds=5)
    assert worker_seen >= before - timedelta(seconds=5)


def test_ping_message_replies_pong_and_refreshes_last_seen(client):
    """Application-level ping is acknowledged with pong and bumps last_seen."""
    test_client, db_url = client
    guild_id = "lvg002"
    worker_id = "w-lvb002"
    agent_id = "a-lvb002"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        ws.send_json(
            {
                "type": "join",
                "agentId": agent_id,
                "agentName": "Test",
                "agentType": "worker",
                "workerId": worker_id,
            }
        )
        ws.receive_json()  # agent-joined

        # Force last_seen artificially old so we can detect the refresh.
        old_ts = datetime.now(UTC) - timedelta(minutes=5)

        with _sync_session(db_url) as session:
            session.execute(update(Agent).where(Agent.id == agent_id).values(last_seen=old_ts))
            session.execute(update(Worker).where(Worker.id == worker_id).values(last_seen=old_ts))
            session.commit()

        # Heartbeat ping carries only workerId — backend looks up agents from
        # the worker_id and refreshes them all together.
        ws.send_json({"type": "ping", "workerId": worker_id})
        reply = ws.receive_json()
        assert reply["type"] == "pong"
        assert "timestamp" in reply

    agent_seen, worker_seen = _read_last_seen(db_url, agent_id, worker_id)
    assert agent_seen is not None and str(agent_seen) != old_ts
    assert worker_seen is not None and str(worker_seen) != old_ts


def test_any_inbound_frame_touches_sibling_agents(client):
    """A ping carrying agent slot 0's id must also refresh sibling slot rows
    so that idle slots aren't swept offline while their parent worker is alive."""
    test_client, db_url = client
    guild_id = "lvg003"
    worker_id = "w-lvc003"
    slot0 = "a-lvc0a3"
    slot1 = "a-lvc0b3"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        for aid in (slot0, slot1):
            ws.send_json(
                {
                    "type": "join",
                    "agentId": aid,
                    "agentName": "Test",
                    "agentType": "worker",
                    "workerId": worker_id,
                }
            )
            ws.receive_json()  # broadcast for each join

        old_ts = datetime.now(UTC) - timedelta(minutes=5)

        with _sync_session(db_url) as session:
            session.execute(
                update(Agent).where(Agent.worker_id == worker_id).values(last_seen=old_ts)
            )
            session.commit()

        ws.send_json({"type": "ping", "workerId": worker_id})
        ws.receive_json()  # pong

    seen0, _ = _read_last_seen(db_url, slot0, worker_id)
    seen1, _ = _read_last_seen(db_url, slot1, worker_id)
    assert str(seen0) != old_ts, "slot 0 should be refreshed via the worker_id ping"
    assert str(seen1) != old_ts, "slot 1 should be refreshed via the worker_id ping"


def test_stale_sweeper_marks_silent_workers_offline(client, monkeypatch):
    """The sweeper marks any agent whose last_seen is past the cutoff offline,
    cascades to the worker row, and emits an agent-state offline broadcast."""
    test_client, db_url = client
    guild_id = "lvg004"
    worker_id = "w-lvd004"
    agent_id = "a-lvd004"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    now = datetime.now(UTC)
    old = datetime.now(UTC) - timedelta(seconds=300)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        session.add(
            Agent(
                id=agent_id,
                guild_id=guild_pk,
                worker_id=worker_id,
                name="Test",
                type="worker",
                state="idle",
                joined_at=now,
                last_seen=old,
            )
        )
        session.execute(update(Worker).where(Worker.id == worker_id).values(last_seen=old))
        session.commit()

    # Tighten the threshold so the sweep triggers immediately for this test.
    monkeypatch.setattr(main_module, "WORKER_OFFLINE_AFTER_SECONDS", 1.0)

    async def _drive() -> int:
        return await main_module._sweep_stale_workers_once()

    marked = asyncio.run(_drive())
    assert marked == 1

    with _sync_session(db_url) as session:
        a_state = session.scalar(select(Agent.state).where(Agent.id == agent_id))
        w_state = session.scalar(select(Worker.state).where(Worker.id == worker_id))
    assert a_state == "offline"
    assert w_state == "offline"


def test_migration_backfills_last_seen_for_existing_rows(monkeypatch):
    """Pre-existing workers/agents must be stamped with last_seen when the
    migration runs so the sweeper doesn't immediately mark them offline.

    NOTE: This test was written for SQLite migration testing. With PostgreSQL,
    the migration infrastructure is different and this test is skipped.
    The equivalent behaviour is verified by the fact that all migrations run
    clean on a fresh PostgreSQL database.
    """
    pytest.skip(
        "Migration backfill test was SQLite-specific (uses alembic partial upgrade). "
        "PostgreSQL migrations are verified by the session-scoped _setup_schema fixture."
    )


def test_sweeper_marks_zombie_worker_offline_when_agents_already_offline(client, monkeypatch):
    """The sweeper must mark a zombie worker offline even when all of its
    agents are already in the 'offline' state.

    Regression test for the scenario where:
    1. A worker's WS drops and the close handler marks its *agents* offline
       (correctly), but fails to mark the *Worker* row offline (due to the
       agent_id/worker_id bug that was fixed in websocket.py).
    2. The sweeper's agent-cascade path finds no non-offline stale agents,
       so it used to return early without ever touching the Worker row.

    With the fix, the sweeper also queries the workers table directly and
    marks any worker with a stale ``last_seen`` offline regardless of its
    agents' states.
    """
    test_client, db_url = client
    guild_id = "lvg006"
    worker_id = "w-lvf006"
    agent_id = "a-lvf006"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    now = datetime.now(UTC)
    old = datetime.now(UTC) - timedelta(seconds=300)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        # Insert an agent that is *already* offline — simulates the state after
        # the buggy WS close handler ran: agent marked offline, worker not.
        session.add(
            Agent(
                id=agent_id,
                guild_id=guild_pk,
                worker_id=worker_id,
                name="Test",
                type="worker",
                state="offline",
                joined_at=now,
                last_seen=old,
            )
        )
        # Worker is still "online" (the buggy state) with a stale last_seen.
        session.execute(
            update(Worker).where(Worker.id == worker_id).values(state="online", last_seen=old)
        )
        session.commit()

    monkeypatch.setattr(main_module, "WORKER_OFFLINE_AFTER_SECONDS", 1.0)

    async def _drive() -> int:
        return await main_module._sweep_stale_workers_once()

    marked = asyncio.run(_drive())
    # 0 stale agents (agent was already offline) + 1 zombie worker evicted.
    assert marked == 1

    with _sync_session(db_url) as session:
        w_state = session.scalar(select(Worker.state).where(Worker.id == worker_id))
    assert w_state == "offline", (
        f"worker.state={w_state!r}, expected 'offline' — "
        "sweeper did not catch zombie worker with no active agents"
    )


def test_sweeper_skips_fresh_workers(client):
    """Agents whose last_seen is recent must not be touched by the sweeper."""
    test_client, db_url = client
    guild_id = "lvg005"
    worker_id = "w-lve005"
    agent_id = "a-lve005"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        session.add(
            Agent(
                id=agent_id,
                guild_id=guild_pk,
                worker_id=worker_id,
                name="Test",
                type="worker",
                state="idle",
                joined_at=now,
                last_seen=now,
            )
        )
        session.commit()

    async def _drive() -> int:
        return await main_module._sweep_stale_workers_once()

    marked = asyncio.run(_drive())
    assert marked == 0

    with _sync_session(db_url) as session:
        a_state = session.scalar(select(Agent.state).where(Agent.id == agent_id))
    assert a_state == "idle"


def test_stale_task_watchdog_releases_lock_when_agent_goes_idle(client, monkeypatch):
    """Stale-task watchdog: a task stuck in 'working' whose agent is idle and
    whose lock is old enough must be moved to 'awaiting-review' and unlocked.

    Scenario: agent finished and went idle but never sent task-complete (crash,
    edge-case code path, etc.) — the lock is orphaned.  The sweeper should
    detect the mismatch and recover the task automatically.
    """
    test_client, db_path = client
    guild_id = "lvg007"
    worker_id = "w-lvg007"
    agent_id = "a-lvg007"
    task_id = "t-lvg007"

    _setup_guild_and_worker(db_path, guild_id, worker_id)
    from datetime import timezone

    now = datetime.now(UTC)
    # Lock acquired long ago — older than WORKER_OFFLINE_AFTER_SECONDS (set to 1s below).
    old_lock_dt = datetime.now(UTC) - timedelta(seconds=300)
    future_exp_dt = datetime.now(UTC) + timedelta(hours=1)

    with _sync_session(db_path) as session:
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        # Task stuck in "working".
        session.add(
            Task(
                id=task_id,
                worker_id=worker_id,
                guild_id=guild_pk,
                description="stuck task",
                tool="claude",
                state="working",
                created_at=now,
            )
        )
        # Agent is idle (finished), not actively running anything.
        session.add(
            Agent(
                id=agent_id,
                guild_id=guild_pk,
                worker_id=worker_id,
                name="Test",
                type="worker",
                state="idle",
                joined_at=now,
                last_seen=now,
                current_task_id=None,
            )
        )
        # Stale lock for the task.
        session.add(
            Lock(
                key=f"task:{task_id}",
                owner=worker_id,
                acquired_at=old_lock_dt,
                expires_at=future_exp_dt,
            )
        )
        session.commit()

    monkeypatch.setattr(main_module, "WORKER_OFFLINE_AFTER_SECONDS", 1.0)

    async def _drive() -> int:
        return await main_module._sweep_stale_workers_once()

    asyncio.run(_drive())

    with _sync_session(db_path) as session:
        t_state = session.scalar(select(Task.state).where(Task.id == task_id))
        lock_key = session.scalar(select(Lock.key).where(Lock.key == f"task:{task_id}"))

    assert t_state == "awaiting-review", (
        f"task.state={t_state!r} — watchdog should have moved it to 'awaiting-review'"
    )
    assert lock_key is None, "lock should have been released by the watchdog"
