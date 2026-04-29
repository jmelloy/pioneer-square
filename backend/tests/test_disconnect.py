"""Tests for the server-side worker-disconnect message handler.

Verifies that when the server receives a worker-disconnect message over
WebSocket, it immediately marks the worker and its agents offline in the
database and broadcasts the offline state — without waiting for the
WebSocket connection to close.

A module-scoped `client` fixture is used so only one TestClient is created
for the whole module, avoiding the starlette/anyio hang that occurs when a
second TestClient tries to start against the same singleton app instance.
"""

from __future__ import annotations

import os
import sys
import sqlite3
from datetime import datetime, timezone

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
    """Module-scoped TestClient — one TestClient for all disconnect tests."""
    tmp_path = tmp_path_factory.mktemp("disconnect_db")
    db_path = str(tmp_path / "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    os.environ["DATABASE_URL"] = db_url
    _create_db(db_path)

    new_engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)

    mp = pytest.MonkeyPatch()
    mp.setattr(database_module, "AsyncSessionLocal", new_session)
    mp.setattr(main_module, "AsyncSessionLocal", new_session)

    async def _stubbed_init_db() -> None:
        pass

    mp.setattr(main_module, "init_db", _stubbed_init_db)
    mp.setenv("DATABASE_URL", db_url)
    mp.setenv("DB_PATH", db_path)

    with TestClient(main_module.app) as c:
        yield c, db_path

    mp.undo()
    os.environ.pop("DATABASE_URL", None)


def _setup_guild_and_worker(db_path: str, guild_id: str, worker_id: str) -> None:
    """Insert a test guild and worker directly into the DB."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO guilds (id, created_at, name) VALUES (?, ?, ?)",
            (guild_id, now, "Test Guild"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO workers (id, guild_id, repos, state, created_at)"
            " VALUES (?, ?, '[]', 'online', ?)",
            (worker_id, guild_id, now),
        )
        conn.commit()


def _get_states(db_path: str, agent_id: str, worker_id: str) -> tuple[str, str]:
    """Return (agent_state, worker_state) from the DB."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        agent_row = conn.execute(
            "SELECT state FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        worker_row = conn.execute(
            "SELECT state FROM workers WHERE id = ?", (worker_id,)
        ).fetchone()
    return (
        agent_row["state"] if agent_row else None,
        worker_row["state"] if worker_row else None,
    )


def test_worker_disconnect_marks_agent_and_worker_offline(client):
    """Server marks agent and worker offline on receiving worker-disconnect."""
    test_client, db_path = client
    guild_id = "gld001"
    worker_id = "w-abc001"
    agent_id = "a-abc001"

    _setup_guild_and_worker(db_path, guild_id, worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        # Join so the server knows this agent belongs to this connection.
        ws.send_json({
            "type": "join",
            "agentId": agent_id,
            "agentName": "Test Worker",
            "agentType": "worker",
            "workerId": worker_id,
        })
        joined_msg = ws.receive_json()
        assert joined_msg["type"] == "agent-joined"
        assert joined_msg["agentId"] == agent_id

        # Send the graceful disconnect notification.
        ws.send_json({
            "type": "worker-disconnect",
            "workerId": worker_id,
        })

        # The server should immediately broadcast an offline state update.
        offline_msg = ws.receive_json()
        assert offline_msg["type"] == "agent-state", (
            f"Expected agent-state broadcast, got: {offline_msg}"
        )
        assert offline_msg["agentId"] == agent_id
        assert offline_msg["state"] == "offline"

    # After the connection closes, both agent and worker should be offline.
    agent_state, worker_state = _get_states(db_path, agent_id, worker_id)
    assert agent_state == "offline", f"agent.state={agent_state!r}, expected 'offline'"
    assert worker_state == "offline", f"worker.state={worker_state!r}, expected 'offline'"


def test_reconnect_does_not_get_clobbered_by_old_finally(client):
    """If a new WebSocket joins for an existing agent before the old WS's
    teardown finishes, the old WS's cleanup must not mark the freshly-online
    agent offline. Otherwise the frontend shows the worker offline forever
    after a reconnect."""
    test_client, db_path = client
    guild_id = "gld003"
    worker_id = "w-rec001"
    agent_id = "a-rec001"

    _setup_guild_and_worker(db_path, guild_id, worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws2:
        # First connection joins as the agent.
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws1:
            ws1.send_json({
                "type": "join",
                "agentId": agent_id,
                "agentName": "Test Worker",
                "agentType": "worker",
                "workerId": worker_id,
            })
            ws1.receive_json()
            ws2.receive_json()  # observer drains broadcast

            # Simulate a reconnect: a new WS takes over the same agent_id.
            ws2.send_json({
                "type": "join",
                "agentId": agent_id,
                "agentName": "Test Worker",
                "agentType": "worker",
                "workerId": worker_id,
            })
            ws2.receive_json()
            ws1.receive_json()  # ws1 sees the second join broadcast

            # ws1 closes when the with-block exits. Its teardown must NOT
            # mark the agent offline because ws2 now owns it.

        # After ws1 closes, but ws2 (the new owner) is still connected, the
        # agent should still be online in the DB.
        agent_state, worker_state = _get_states(db_path, agent_id, worker_id)
        assert agent_state != "offline", (
            f"Old WS's cleanup wrongly marked the reconnected agent offline: "
            f"agent.state={agent_state!r}"
        )
        assert worker_state != "offline", (
            f"Old WS's cleanup wrongly marked the reconnected worker offline: "
            f"worker.state={worker_state!r}"
        )



def test_worker_disconnect_without_prior_join_is_harmless(client):
    """worker-disconnect with no joined agents must not crash the server.

    Without a prior join, joined_agents is empty so there is nothing to
    broadcast; we just verify the server does not raise an exception.
    The worker-row DB update is covered by the first test, which uses join
    to create a synchronization point via the broadcast response.
    """
    test_client, db_path = client
    guild_id = "gld002"
    worker_id = "w-xyz002"

    _setup_guild_and_worker(db_path, guild_id, worker_id)

    # Should complete without raising any exception.
    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        ws.send_json({
            "type": "worker-disconnect",
            "workerId": worker_id,
        })
