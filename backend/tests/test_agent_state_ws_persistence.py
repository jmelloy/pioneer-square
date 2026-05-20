"""Backend persists ``current_task_id`` from agent-state and forwards it.

Guards the contract that anchors the frontend's task→agent matching:

- ``agent-state`` with ``taskId`` writes ``current_task_id`` on the agents row
  and forwards ``taskId``/``workerId`` to other listeners on the guild.
- Idle/offline transitions clear ``current_task_id`` even if the worker
  forgot to send ``taskId=None`` — the field can't lag a real state change.
- The page-load REST snapshot (`GET /guilds/{id}`) returns
  ``current_task_id`` so a reload mid-task shows the right slot at the
  right bench without waiting for the next WS update.

Patterned on test_pr_url_ws_persistence.py: module-scoped TestClient,
per-test guild ids, DB checked while the connections are still open
(commit happens before broadcast).
"""

from __future__ import annotations

import os
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
from helpers import raw_conn, truncate_all
from starlette.testclient import TestClient  # noqa: E402

# Hardcoded default is intentional — test/local-dev environment only.
# In CI, set TEST_DATABASE_URL to the postgres service URL.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pioneer:pioneer_password@localhost/pioneer_test",
)


@pytest.fixture(scope="function")
def client():
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


def _insert_guild_worker_task(
    db_url: str,
    *,
    guild_id: str,
    worker_id: str,
    task_id: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "INSERT INTO guilds (guild_id, created_at, name) VALUES (%s, %s, %s) RETURNING id",
            (guild_id, now, "Test Guild"),
        )
        guild_pk = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO workers (id, guild_pk, repos, state, created_at)"
            " VALUES (%s, %s, '[]', 'online', %s)",
            (worker_id, guild_pk, now),
        )
        # Use "awaiting-review" so the join handler does not replay the task as
        # task-assigned (it only replays "pending" and "working" tasks). For
        # these tests the task state is incidental — we're checking the
        # agent-state path, not the task lifecycle.
        cur.execute(
            "INSERT INTO tasks (id, worker_id, guild_pk, description, tool, state, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (task_id, worker_id, guild_pk, "test task", "claude", "awaiting-review", now),
        )


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


def test_agent_state_persists_current_task_id(client):
    """`agent-state` with taskId writes ``current_task_id`` to the agents row."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-1", "w-gas1", "t-gas1", "a-gas1"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs1", "w-obs1")
            ws_worker.receive_json()  # drain obs's agent-joined broadcast

            ws_worker.send_json(
                {
                    "type": "agent-state",
                    "workerId": worker_id,
                    "agentId": agent_id,
                    "taskId": task_id,
                    "state": "working",
                    "activity": "editing",
                }
            )
            msg = ws_obs.receive_json()
            assert msg["type"] == "agent-state"
            # Forwarded for the frontend; this is what drives the bench match.
            assert msg["agentId"] == agent_id
            assert msg["workerId"] == worker_id
            assert msg["taskId"] == task_id
            assert msg["state"] == "working"
            assert msg["activity"] == "editing"

            with raw_conn(db_url) as (conn, cur):
                cur.execute(
                    "SELECT state, activity, current_task_id FROM agents WHERE id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()

    assert row["state"] == "working"
    assert row["activity"] == "editing"
    assert row["current_task_id"] == task_id


def test_agent_state_idle_clears_current_task_id(client):
    """Going idle must null ``current_task_id`` even if taskId is omitted."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-2", "w-gas2", "t-gas2", "a-gas2"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs2", "w-obs2")
            ws_worker.receive_json()

            # First put the slot into 'working' on a task.
            ws_worker.send_json(
                {
                    "type": "agent-state",
                    "workerId": worker_id,
                    "agentId": agent_id,
                    "taskId": task_id,
                    "state": "working",
                }
            )
            ws_obs.receive_json()

            # Then go idle without sending taskId — the handler still clears it.
            ws_worker.send_json(
                {
                    "type": "agent-state",
                    "workerId": worker_id,
                    "agentId": agent_id,
                    "state": "idle",
                }
            )
            msg = ws_obs.receive_json()
            assert msg["state"] == "idle"
            assert msg["taskId"] is None
            assert msg["activity"] is None

            with raw_conn(db_url) as (conn, cur):
                cur.execute(
                    "SELECT state, activity, current_task_id FROM agents WHERE id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()

    assert row["state"] == "idle"
    assert row["activity"] is None
    assert row["current_task_id"] is None


def test_agent_state_explicit_task_id_null_clears(client):
    """Explicit ``taskId: null`` in any state clears ``current_task_id``."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-3", "w-gas3", "t-gas3", "a-gas3"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs3", "w-obs3")
            ws_worker.receive_json()

            ws_worker.send_json(
                {
                    "type": "agent-state",
                    "workerId": worker_id,
                    "agentId": agent_id,
                    "taskId": task_id,
                    "state": "working",
                }
            )
            ws_obs.receive_json()

            # Same state, but the worker explicitly says "no task" — handler
            # must honour the null even though state isn't idle/offline.
            ws_worker.send_json(
                {
                    "type": "agent-state",
                    "workerId": worker_id,
                    "agentId": agent_id,
                    "taskId": None,
                    "state": "working",
                }
            )
            msg = ws_obs.receive_json()
            assert msg["taskId"] is None

            with raw_conn(db_url) as (conn, cur):
                cur.execute("SELECT current_task_id FROM agents WHERE id = %s", (agent_id,))
                row = cur.fetchone()

    assert row["current_task_id"] is None


def test_guild_get_returns_current_task_id(client):
    """REST snapshot exposes ``current_task_id`` so a page reload mid-task
    can map the bench to the right slot without waiting for the next WS event."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-4", "w-gas4", "t-gas4", "a-gas4"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    headers = {"Authorization": "Bearer test-token"}
    # Seed the test token & guild membership the way other tests do.
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "INSERT INTO users (id, github_id, github_login, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            ("u-gas4", "999004", "tester", now, now),
        )
        cur.execute(
            "INSERT INTO user_sessions (token, github_user_id, created_at)"
            " VALUES (%s, %s, %s) ON CONFLICT (token) DO UPDATE SET github_user_id = EXCLUDED.github_user_id",
            ("test-token", "u-gas4", now),
        )
        cur.execute("SELECT id FROM guilds WHERE guild_id = %s", (guild_id,))
        guild_pk = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO guild_members (guild_pk, user_id, role, created_at)"
            " VALUES (%s, %s, 'member', %s) ON CONFLICT (guild_pk, user_id) DO NOTHING",
            (guild_pk, "u-gas4", now),
        )

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs4", "w-obs4")
            ws_worker.receive_json()

            ws_worker.send_json(
                {
                    "type": "agent-state",
                    "workerId": worker_id,
                    "agentId": agent_id,
                    "taskId": task_id,
                    "state": "working",
                    "activity": "editing",
                }
            )
            ws_obs.receive_json()  # ensures handler committed before REST read

            resp = test_client.get(f"/guilds/{guild_id}", headers=headers)
            assert resp.status_code == 200
            body = resp.json()
            agents_by_id = {a["id"]: a for a in body["agents"]}
            assert agent_id in agents_by_id
            assert agents_by_id[agent_id]["current_task_id"] == task_id
            assert agents_by_id[agent_id]["state"] == "working"
            assert agents_by_id[agent_id]["activity"] == "editing"
