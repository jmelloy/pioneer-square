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

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
from _test_config import TEST_DATABASE_URL  # noqa: E402
from helpers import _sync_session, insert_guild, insert_worker  # noqa: E402
from models import Agent, Worker  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlmodel import col  # noqa: E402


@pytest.fixture(scope="module")
def client(_setup_schema):
    """Module-scoped TestClient — one TestClient for all disconnect tests."""
    db_url = TEST_DATABASE_URL

    new_engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    new_session = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)

    mp = pytest.MonkeyPatch()
    mp.setattr(database_module, "AsyncSessionLocal", new_session)
    mp.setattr(main_module, "AsyncSessionLocal", new_session)

    async def _stubbed_reset_connection_state() -> None:
        pass

    mp.setattr(main_module, "reset_connection_state", _stubbed_reset_connection_state)

    async def _stubbed_drain_stale_workers_on_startup():
        return []

    mp.setattr(
        main_module, "drain_stale_workers_on_startup", _stubbed_drain_stale_workers_on_startup
    )
    mp.setenv("DATABASE_URL", db_url)

    with TestClient(main_module.app) as c:
        yield c, db_url

    mp.undo()


def _setup_guild_and_worker(db_url: str, guild_id: str, worker_id: str) -> None:
    """Insert a test guild and worker directly into the DB."""
    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")


def _get_states(db_url: str, agent_id: str, worker_id: str) -> tuple[str | None, str | None]:
    """Return (agent_state, worker_state) from the DB."""
    with _sync_session(db_url) as session:
        agent_state = session.scalar(select(col(Agent.state)).where(col(Agent.id) == agent_id))
        worker_state = session.scalar(select(col(Worker.state)).where(col(Worker.id) == worker_id))
    return (agent_state, worker_state)


def test_worker_disconnect_marks_agent_and_worker_offline(client):
    """Server marks agent and worker offline on receiving worker-disconnect."""
    test_client, db_url = client
    guild_id = "dco001"
    worker_id = "w-abc001"
    agent_id = "a-abc001"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        # Join so the server knows this agent belongs to this connection.
        ws.send_json(
            {
                "type": "join",
                "agentId": agent_id,
                "agentName": "Test Worker",
                "agentType": "worker",
                "workerId": worker_id,
            }
        )
        joined_msg = ws.receive_json()
        assert joined_msg["type"] == "agent-joined"
        assert joined_msg["agentId"] == agent_id

        # Send the graceful disconnect notification.
        ws.send_json(
            {
                "type": "worker-disconnect",
                "workerId": worker_id,
            }
        )

        # The server should immediately broadcast an offline state update.
        offline_msg = ws.receive_json()
        assert offline_msg["type"] == "agent-state", (
            f"Expected agent-state broadcast, got: {offline_msg}"
        )
        assert offline_msg["agentId"] == agent_id
        assert offline_msg["state"] == "offline"

    # After the connection closes, both agent and worker should be offline.
    agent_state, worker_state = _get_states(db_url, agent_id, worker_id)
    assert agent_state == "offline", f"agent.state={agent_state!r}, expected 'offline'"
    assert worker_state == "offline", f"worker.state={worker_state!r}, expected 'offline'"


def test_reconnect_does_not_get_clobbered_by_old_finally(client):
    """If a new WebSocket joins for an existing agent before the old WS's
    teardown finishes, the old WS's cleanup must not mark the freshly-online
    agent offline. Otherwise the frontend shows the worker offline forever
    after a reconnect."""
    test_client, db_url = client
    guild_id = "dco003"
    worker_id = "w-rec001"
    agent_id = "a-rec001"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws2:
        # First connection joins as the agent.
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws1:
            ws1.send_json(
                {
                    "type": "join",
                    "agentId": agent_id,
                    "agentName": "Test Worker",
                    "agentType": "worker",
                    "workerId": worker_id,
                }
            )
            ws1.receive_json()
            ws2.receive_json()  # observer drains broadcast

            # Simulate a reconnect: a new WS takes over the same agent_id.
            ws2.send_json(
                {
                    "type": "join",
                    "agentId": agent_id,
                    "agentName": "Test Worker",
                    "agentType": "worker",
                    "workerId": worker_id,
                }
            )
            ws2.receive_json()
            ws1.receive_json()  # ws1 sees the second join broadcast

            # ws1 closes when the with-block exits. Its teardown must NOT
            # mark the agent offline because ws2 now owns it.

        # After ws1 closes, but ws2 (the new owner) is still connected, the
        # agent should still be online in the DB.
        agent_state, worker_state = _get_states(db_url, agent_id, worker_id)
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
    test_client, db_url = client
    guild_id = "dco002"
    worker_id = "w-xyz002"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    # Should complete without raising any exception.
    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        ws.send_json(
            {
                "type": "worker-disconnect",
                "workerId": worker_id,
            }
        )


def test_abrupt_disconnect_marks_worker_offline(client):
    """When the WebSocket drops without a graceful worker-disconnect, the
    server's finally-block cleanup must mark both the agent AND the worker
    offline.

    Regression test for the bug where the finally block used ``agent_id``
    (e.g. "a-…") as the filter for ``Worker.id`` (which stores "w-…" IDs),
    causing the Worker row to stay "online" after an abrupt container death.
    """
    test_client, db_url = client
    guild_id = "gld004"
    worker_id = "w-abrupt04"
    agent_id = "a-abrupt04"

    _setup_guild_and_worker(db_url, guild_id, worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
        ws.send_json(
            {
                "type": "join",
                "agentId": agent_id,
                "agentName": "Test Worker",
                "agentType": "worker",
                "workerId": worker_id,
            }
        )
        ws.receive_json()  # agent-joined broadcast
        # Close without sending worker-disconnect — simulates abrupt container death.

    # The finally block must have marked both offline.
    agent_state, worker_state = _get_states(db_url, agent_id, worker_id)
    assert agent_state == "offline", f"agent.state={agent_state!r}, expected 'offline'"
    assert worker_state == "offline", (
        f"worker.state={worker_state!r}, expected 'offline' — "
        "finally block likely used agent_id instead of worker_id for Worker update"
    )
