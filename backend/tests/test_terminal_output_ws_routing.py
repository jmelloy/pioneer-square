"""Backend terminal-output payloads preserve worker-level ownership.

Worker-wide emits should use workerId only, and the backend should store and
broadcast that workerId without inventing an agentId. Task-scoped emits still
carry both workerId and agentId so the frontend can render both views.
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
from _test_config import TEST_DATABASE_URL  # noqa: E402
from helpers import insert_guild, insert_worker  # noqa: E402
from models import TaskLog  # noqa: E402
from sqlalchemy import select  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client(_setup_schema):
    """Module-scoped TestClient against the shared PostgreSQL test database."""
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


def _insert_guild_worker(db_url: str, *, guild_id: str, worker_id: str) -> None:
    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")


def _join_ws(ws, agent_id: str, worker_id: str | None = None) -> None:
    """Join a guild WebSocket.

    Pass ``worker_id`` to join as a worker (FK-safe: must exist in workers
    table).  Omit it to join as a browser observer — no workers row needed.
    """
    msg: dict = {
        "type": "join",
        "agentId": agent_id,
        "agentName": "Test Slot",
        "agentType": "worker" if worker_id else "browser",
    }
    if worker_id:
        msg["workerId"] = worker_id
    ws.send_json(msg)
    while True:
        recv = ws.receive_json()
        if recv["type"] == "agent-joined":
            break


def test_worker_only_terminal_output_uses_worker_id_only(client):
    test_client, db_url = client
    guild_id, worker_id, agent_id = "gto-1", "w-gto1", "a-gto1"
    _insert_guild_worker(db_url, guild_id=guild_id, worker_id=worker_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs1")  # browser observer — no workerId needed
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

            from helpers import _sync_session

            with _sync_session(db_url) as session:
                row = session.execute(
                    select(TaskLog.worker_id, TaskLog.agent_id, TaskLog.task_id, TaskLog.line)
                    .order_by(TaskLog.id.desc())
                    .limit(1)
                ).first()

    assert row.worker_id == worker_id
    assert row.agent_id is None
    assert row.task_id is None
    assert row.line == "[worker] Pulled repo"
