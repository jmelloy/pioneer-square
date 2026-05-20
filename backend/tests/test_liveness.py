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
import sqlite3
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
from helpers import create_db as _create_db  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """Module-scoped TestClient — one client per module to dodge the
    starlette/anyio hang seen when a second TestClient is started against the
    same singleton FastAPI app instance."""
    tmp_path = tmp_path_factory.mktemp("liveness_db")
    db_path = str(tmp_path / "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    os.environ["DATABASE_URL"] = db_url
    _create_db(db_path)

    new_engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)

    mp = pytest.MonkeyPatch()
    mp.setattr(database_module, "AsyncSessionLocal", new_session)
    mp.setattr(main_module, "AsyncSessionLocal", new_session)

    async def _stubbed_reset_connection_state() -> None:
        pass

    mp.setattr(main_module, "reset_connection_state", _stubbed_reset_connection_state)
    mp.setenv("DATABASE_URL", db_url)
    mp.setenv("DB_PATH", db_path)

    with TestClient(main_module.app) as c:
        yield c, db_path

    mp.undo()
    os.environ.pop("DATABASE_URL", None)


def _setup_guild_and_worker(db_path: str, guild_id: str, worker_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO guilds (guild_id, created_at, name) VALUES (?, ?, ?)",
            (guild_id, now, "Test Guild"),
        )
        row = conn.execute(
            "SELECT id FROM guilds WHERE guild_id = ? AND deleted_at IS NULL", (guild_id,)
        ).fetchone()
        guild_pk = row[0]
        conn.execute(
            "INSERT OR IGNORE INTO workers (id, guild_pk, repos, state, created_at)"
            " VALUES (?, ?, '[]', 'online', ?)",
            (worker_id, guild_pk, now),
        )
        conn.commit()


def _read_last_seen(db_path: str, agent_id: str, worker_id: str) -> tuple[str | None, str | None]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        agent = conn.execute("SELECT last_seen FROM agents WHERE id = ?", (agent_id,)).fetchone()
        worker = conn.execute("SELECT last_seen FROM workers WHERE id = ?", (worker_id,)).fetchone()
    return (agent["last_seen"] if agent else None, worker["last_seen"] if worker else None)


def test_join_initialises_last_seen(client):
    """A worker join sets last_seen on both the agent and worker rows."""
    test_client, db_path = client
    guild_id = "lvg001"
    worker_id = "w-lva001"
    agent_id = "a-lva001"

    _setup_guild_and_worker(db_path, guild_id, worker_id)
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

    agent_seen, worker_seen = _read_last_seen(db_path, agent_id, worker_id)
    assert agent_seen is not None
    assert worker_seen is not None
    assert datetime.fromisoformat(agent_seen) >= before - timedelta(seconds=5)
    assert datetime.fromisoformat(worker_seen) >= before - timedelta(seconds=5)


def test_ping_message_replies_pong_and_refreshes_last_seen(client):
    """Application-level ping is acknowledged with pong and bumps last_seen."""
    test_client, db_path = client
    guild_id = "lvg002"
    worker_id = "w-lvb002"
    agent_id = "a-lvb002"

    _setup_guild_and_worker(db_path, guild_id, worker_id)

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
        old_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (old_ts, agent_id))
            conn.execute("UPDATE workers SET last_seen = ? WHERE id = ?", (old_ts, worker_id))
            conn.commit()

        # Heartbeat ping carries only workerId — backend looks up agents from
        # the worker_id and refreshes them all together.
        ws.send_json({"type": "ping", "workerId": worker_id})
        reply = ws.receive_json()
        assert reply["type"] == "pong"
        assert "timestamp" in reply

    agent_seen, worker_seen = _read_last_seen(db_path, agent_id, worker_id)
    assert agent_seen is not None and agent_seen != old_ts
    assert worker_seen is not None and worker_seen != old_ts


def test_any_inbound_frame_touches_sibling_agents(client):
    """A ping carrying agent slot 0's id must also refresh sibling slot rows
    so that idle slots aren't swept offline while their parent worker is alive."""
    test_client, db_path = client
    guild_id = "lvg003"
    worker_id = "w-lvc003"
    slot0 = "a-lvc0a3"
    slot1 = "a-lvc0b3"

    _setup_guild_and_worker(db_path, guild_id, worker_id)

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

        old_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE agents SET last_seen = ? WHERE worker_id = ?", (old_ts, worker_id))
            conn.commit()

        ws.send_json({"type": "ping", "workerId": worker_id})
        ws.receive_json()  # pong

    seen0, _ = _read_last_seen(db_path, slot0, worker_id)
    seen1, _ = _read_last_seen(db_path, slot1, worker_id)
    assert seen0 != old_ts, "slot 0 should be refreshed via the worker_id ping"
    assert seen1 != old_ts, "slot 1 should be refreshed via the worker_id ping"


def test_stale_sweeper_marks_silent_workers_offline(client, monkeypatch):
    """The sweeper marks any agent whose last_seen is past the cutoff offline,
    cascades to the worker row, and emits an agent-state offline broadcast."""
    test_client, db_path = client
    guild_id = "lvg004"
    worker_id = "w-lvd004"
    agent_id = "a-lvd004"

    _setup_guild_and_worker(db_path, guild_id, worker_id)
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM guilds WHERE guild_id = ? AND deleted_at IS NULL", (guild_id,)
        ).fetchone()
        guild_pk = row[0]
        conn.execute(
            "INSERT INTO agents "
            "(id, guild_pk, worker_id, name, type, state, joined_at, last_seen) "
            "VALUES (?, ?, ?, 'Test', 'worker', 'idle', ?, ?)",
            (agent_id, guild_pk, worker_id, now, now),
        )
        # Backdate last_seen so the sweeper considers it stale.
        old = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
        conn.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (old, agent_id))
        conn.execute("UPDATE workers SET last_seen = ? WHERE id = ?", (old, worker_id))
        conn.commit()

    # Open an observer connection to capture the offline broadcast.
    with test_client.websocket_connect(f"/ws/{guild_id}") as observer:
        # Tighten the threshold so the sweep triggers immediately for this test.
        monkeypatch.setattr(main_module, "WORKER_OFFLINE_AFTER_SECONDS", 1.0)

        async def _drive() -> int:
            return await main_module._sweep_stale_workers_once()

        marked = asyncio.run(_drive())
        assert marked == 1

        offline_msg = observer.receive_json()
        assert offline_msg["type"] == "agent-state"
        assert offline_msg["agentId"] == agent_id
        assert offline_msg["state"] == "offline"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        a = conn.execute("SELECT state FROM agents WHERE id = ?", (agent_id,)).fetchone()
        w = conn.execute("SELECT state FROM workers WHERE id = ?", (worker_id,)).fetchone()
    assert a["state"] == "offline"
    assert w["state"] == "offline"


def test_migration_backfills_last_seen_for_existing_rows(tmp_path, monkeypatch):
    """Pre-existing workers/agents must be stamped with last_seen when the
    migration runs so the sweeper doesn't immediately mark them offline."""
    import sqlite3

    from alembic import command
    from alembic.config import Config as AlembicConfig

    db_path = str(tmp_path / "premigrate.db")
    sync_url = f"sqlite+aiosqlite:///{db_path}"
    alembic_ini = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")

    # alembic/env.py reads DATABASE_URL ahead of alembic.ini, so override it
    # for both stages of the migration.
    monkeypatch.setenv("DATABASE_URL", sync_url)

    # Bring the DB only up to the previous head, then insert legacy rows that
    # have no last_seen column yet.
    cfg = AlembicConfig(alembic_ini)
    command.upgrade(cfg, "c4d5e6f7a8b9")

    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO guilds (id, created_at, name) VALUES ('mig001', ?, 'Mig')",
            (now,),
        )
        conn.execute(
            "INSERT INTO workers (id, guild_id, repos, state, created_at) "
            "VALUES ('w-mig001', 'mig001', '[]', 'online', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, guild_id, worker_id, name, type, state, joined_at) "
            "VALUES ('a-mig001', 'mig001', 'w-mig001', 'M', 'worker', 'idle', ?)",
            (now,),
        )
        conn.commit()

    # Apply the new head migration on the same DB.
    command.upgrade(cfg, "head")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        w = conn.execute("SELECT last_seen FROM workers WHERE id = 'w-mig001'").fetchone()
        a = conn.execute("SELECT last_seen FROM agents WHERE id = 'a-mig001'").fetchone()
    assert w["last_seen"] is not None, "worker.last_seen should be backfilled by the migration"
    assert a["last_seen"] is not None, "agent.last_seen should be backfilled by the migration"
    # Stamped ~now (within a generous window for slow CI machines).
    assert datetime.fromisoformat(w["last_seen"]) >= datetime.now(UTC) - timedelta(minutes=5)


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
    test_client, db_path = client
    guild_id = "lvg006"
    worker_id = "w-lvf006"
    agent_id = "a-lvf006"

    _setup_guild_and_worker(db_path, guild_id, worker_id)
    now = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM guilds WHERE guild_id = ? AND deleted_at IS NULL", (guild_id,)
        ).fetchone()
        guild_pk = row[0]
        # Insert an agent that is *already* offline — simulates the state after
        # the buggy WS close handler ran: agent marked offline, worker not.
        conn.execute(
            "INSERT INTO agents "
            "(id, guild_pk, worker_id, name, type, state, joined_at, last_seen) "
            "VALUES (?, ?, ?, 'Test', 'worker', 'offline', ?, ?)",
            (agent_id, guild_pk, worker_id, now, old),
        )
        # Worker is still "online" (the buggy state) with a stale last_seen.
        conn.execute(
            "UPDATE workers SET state = 'online', last_seen = ? WHERE id = ?",
            (old, worker_id),
        )
        conn.commit()

    with test_client.websocket_connect(f"/ws/{guild_id}"):
        monkeypatch.setattr(main_module, "WORKER_OFFLINE_AFTER_SECONDS", 1.0)

        async def _drive() -> int:
            return await main_module._sweep_stale_workers_once()

        marked = asyncio.run(_drive())
        # No non-offline agents were swept (agent was already offline).
        assert marked == 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        w = conn.execute("SELECT state FROM workers WHERE id = ?", (worker_id,)).fetchone()
    assert w["state"] == "offline", (
        f"worker.state={w['state']!r}, expected 'offline' — "
        "sweeper did not catch zombie worker with no active agents"
    )


def test_sweeper_skips_fresh_workers(client):
    """Agents whose last_seen is recent must not be touched by the sweeper."""
    test_client, db_path = client
    guild_id = "lvg005"
    worker_id = "w-lve005"
    agent_id = "a-lve005"

    _setup_guild_and_worker(db_path, guild_id, worker_id)
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM guilds WHERE guild_id = ? AND deleted_at IS NULL", (guild_id,)
        ).fetchone()
        guild_pk = row[0]
        conn.execute(
            "INSERT INTO agents "
            "(id, guild_pk, worker_id, name, type, state, joined_at, last_seen) "
            "VALUES (?, ?, ?, 'Test', 'worker', 'idle', ?, ?)",
            (agent_id, guild_pk, worker_id, now, now),
        )
        conn.commit()

    async def _drive() -> int:
        return await main_module._sweep_stale_workers_once()

    marked = asyncio.run(_drive())
    assert marked == 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        a = conn.execute("SELECT state FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert a["state"] == "idle"
