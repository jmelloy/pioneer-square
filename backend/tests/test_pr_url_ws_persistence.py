"""Tests that pr_url is persisted to the DB when the worker reports it via WebSocket.

Covers two paths:
- ``task-complete``  — primary success path
- ``task-followup-done`` — follow-up iteration path

Both handlers previously read prUrl from the message but never wrote it to the
tasks table; these tests verify the fix.

DB commit happens *before* the broadcast, so we check the database while the
WebSocket connections are still open (after receiving the broadcast message) to
avoid racing against the background tasks the handler spawns after the broadcast.

A module-scoped TestClient is used to avoid the anyio hang that occurs when
multiple TestClient instances start against the same singleton FastAPI app.
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
from _test_config import TEST_DATABASE_URL  # noqa: E402
from helpers import create_db as _create_db  # noqa: E402
from helpers import raw_conn, truncate_all
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
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
            "INSERT INTO guilds (guild_id, created_at, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (guild_id, now, "Test Guild"),
        )
        cur.execute("SELECT id FROM guilds WHERE guild_id = %s AND deleted_at IS NULL", (guild_id,))
        row = cur.fetchone()
        guild_pk = row["id"]
        cur.execute(
            "INSERT INTO workers (id, guild_pk, repos, state, created_at)"
            " VALUES (%s, %s, '[]', 'online', %s) ON CONFLICT DO NOTHING",
            (worker_id, guild_pk, now),
        )
        # Use "awaiting-review" so the join handler does not replay the task as
        # task-assigned (it only replays "pending" and "working" tasks).
        cur.execute(
            "INSERT INTO tasks (id, worker_id, guild_pk, description, tool, state, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (task_id, worker_id, guild_pk, "test task", "claude", "awaiting-review", now),
        )


def _join_ws(ws, agent_id: str, worker_id: str) -> None:
    ws.send_json(
        {
            "type": "join",
            "agentId": agent_id,
            "agentName": "Test Worker",
            "agentType": "worker",
            "workerId": worker_id,
        }
    )
    msg = ws.receive_json()
    assert msg["type"] == "agent-joined"


# ---------------------------------------------------------------------------
# task-complete persists pr_url
# ---------------------------------------------------------------------------


def test_task_complete_persists_pr_url(client):
    """task-complete with prUrl writes pr_url, pr_number, pr_repo to the task row.

    DB commit happens before the broadcast; we check the DB while the WS
    connections are still open so we don't race the post-broadcast background tasks.
    """
    test_client, db_url = client
    guild_id, worker_id, task_id = "gwspr1", "w-wspr1", "t-wspr1"
    agent_id = "a-wspr1"

    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)

        # Open a second connection to capture the broadcast (task-complete goes to
        # all except the sender, so an observer drives the async handler forward).
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs1", "w-obs1")
            ws_worker.receive_json()  # observer's agent-joined broadcast to worker

            ws_worker.send_json(
                {
                    "type": "task-complete",
                    "workerId": worker_id,
                    "taskId": task_id,
                    "branch": "feature/test-pr",
                    "description": "did a thing",
                    "prUrl": "https://github.com/owner/repo/pull/42",
                    "sessionId": "",
                    "lastText": "",
                }
            )
            # Observer receives the broadcast — DB was committed before this point.
            msg = ws_obs.receive_json()
            assert msg["type"] == "task-complete"

            # Check DB while connections are still open to avoid racing background tasks.
            with raw_conn(db_url) as (conn, cur):
                cur.execute(
                    "SELECT pr_url, pr_number, pr_repo, state FROM tasks WHERE id = %s",
                    (task_id,),
                )
                row = cur.fetchone()

    assert row is not None
    assert row["pr_url"] == "https://github.com/owner/repo/pull/42"
    assert row["pr_number"] == 42
    assert row["pr_repo"] == "owner/repo"
    # state stays "awaiting-review" (the .where(state=="working") guard is a no-op here)
    assert row["state"] == "awaiting-review"


def test_task_complete_without_pr_url_leaves_pr_url_null(client):
    """task-complete without prUrl must not write anything to pr_url."""
    test_client, db_url = client
    guild_id, worker_id, task_id = "gwspr2", "w-wspr2", "t-wspr2"
    agent_id = "a-wspr2"

    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)

        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs2", "w-obs2")
            ws_worker.receive_json()

            ws_worker.send_json(
                {
                    "type": "task-complete",
                    "workerId": worker_id,
                    "taskId": task_id,
                    "branch": "feature/no-pr",
                    "description": "no pr task",
                    "prUrl": "",
                    "sessionId": "",
                    "lastText": "",
                }
            )
            ws_obs.receive_json()

            with raw_conn(db_url) as (conn, cur):
                cur.execute(
                    "SELECT pr_url, pr_number, pr_repo FROM tasks WHERE id = %s", (task_id,)
                )
                row = cur.fetchone()

    assert row is not None
    assert row["pr_url"] is None  # pr_url stays NULL
    assert row["pr_number"] is None  # pr_number stays NULL
    assert row["pr_repo"] is None  # pr_repo stays NULL


# ---------------------------------------------------------------------------
# task-followup-done persists pr_url
# ---------------------------------------------------------------------------


def test_task_followup_done_persists_pr_url(client):
    """task-followup-done with prUrl writes pr_url, pr_number, pr_repo to the task row."""
    test_client, db_url = client
    guild_id, worker_id, task_id = "gwspr3", "w-wspr3", "t-wspr3"
    agent_id = "a-wspr3"

    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)

        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs3", "w-obs3")
            ws_worker.receive_json()

            ws_worker.send_json(
                {
                    "type": "task-followup-done",
                    "workerId": worker_id,
                    "taskId": task_id,
                    "success": True,
                    "stopReason": "end_turn",
                    "branch": "feature/test-pr",
                    "sessionId": "",
                    "prUrl": "https://github.com/owner/repo/pull/99",
                }
            )
            msg = ws_obs.receive_json()
            assert msg["type"] == "task-followup-done"

            with raw_conn(db_url) as (conn, cur):
                cur.execute(
                    "SELECT pr_url, pr_number, pr_repo, state FROM tasks WHERE id = %s",
                    (task_id,),
                )
                row = cur.fetchone()

    assert row is not None
    assert row["pr_url"] == "https://github.com/owner/repo/pull/99"
    assert row["pr_number"] == 99
    assert row["pr_repo"] == "owner/repo"
    assert row["state"] == "awaiting-review"
