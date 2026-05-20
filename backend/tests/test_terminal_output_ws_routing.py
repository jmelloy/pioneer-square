"""Backend terminal-output payloads preserve worker-level ownership.

Worker-wide emits should use workerId only, and the backend should store and
broadcast that workerId without inventing an agentId. Task-scoped emits still
carry both workerId and agentId so the frontend can render both views.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
from helpers import create_db as _create_db  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("terminal_output_ws_db")
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


def _insert_guild_worker(db_path: str, *, guild_id: str, worker_id: str) -> None:
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


def _join_ws(ws, agent_id: str, worker_id: str) -> None:
    ws.send_json(
        {
            "type": "join",
            "agentId": agent_id,
            "agentName": "Test Slot",
            "agentType": "worker",
            "workerId": worker_id,
        }
    )
    msg = ws.receive_json()
    assert msg["type"] == "agent-joined"


def test_worker_only_terminal_output_uses_worker_id_only(client):
    test_client, db_path = client
    guild_id, worker_id, agent_id = "gto-1", "w-gto1", "a-gto1"
    _insert_guild_worker(db_path, guild_id=guild_id, worker_id=worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs1", "w-obs1")
            ws_worker.receive_json()  # drain observer join broadcast

            ws_worker.send_json(
                {
                    "type": "terminal-output",
                    "workerId": worker_id,
                    "line": "[worker] Pulled repo",
                }
            )

            msg = ws_obs.receive_json()
            assert msg["type"] == "terminal-output"
            assert msg["workerId"] == worker_id
            assert msg.get("agentId") is None
            assert msg.get("taskId") is None

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT worker_id, agent_id, task_id, line FROM task_logs ORDER BY id DESC LIMIT 1"
                ).fetchone()

    assert row == (worker_id, None, None, "[worker] Pulled repo")
