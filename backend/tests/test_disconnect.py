"""Tests for the server-side worker-disconnect message handler.

Verifies that when the server receives a worker-disconnect message over
WebSocket, it immediately marks the worker and its agents offline in the
database and broadcasts the offline state — without waiting for the
WebSocket connection to close.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import aiosqlite
import pytest

# Ensure backend/ is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as main_module  # noqa: E402  (after sys.path insert)
from starlette.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by a fresh temporary SQLite database."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(main_module, "DB_PATH", db_path)
    with TestClient(main_module.app) as c:
        yield c, db_path


def _setup_guild_and_worker(db_path: str, guild_id: str, worker_id: str) -> None:
    """Insert a test guild and worker directly into the DB."""
    now = datetime.now(timezone.utc).isoformat()

    async def _run():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO guilds (id, created_at, name) VALUES (?, ?, ?)",
                (guild_id, now, "Test Guild"),
            )
            await db.execute(
                "INSERT OR IGNORE INTO workers (id, guild_id, repos, state, created_at)"
                " VALUES (?, ?, '[]', 'online', ?)",
                (worker_id, guild_id, now),
            )
            await db.commit()

    asyncio.run(_run())


def _get_states(db_path: str, agent_id: str, worker_id: str) -> tuple[str, str]:
    """Return (agent_state, worker_state) from the DB."""

    async def _run():
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT state FROM agents WHERE id = ?", (agent_id,)
            ) as cur:
                agent_row = await cur.fetchone()
            async with db.execute(
                "SELECT state FROM workers WHERE id = ?", (worker_id,)
            ) as cur:
                worker_row = await cur.fetchone()
        return (
            agent_row["state"] if agent_row else None,
            worker_row["state"] if worker_row else None,
        )

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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
