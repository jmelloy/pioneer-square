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
    tmp_path = tmp_path_factory.mktemp("pr_url_ws_db")
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


def _insert_guild_worker_task(
    db_path: str,
    *,
    guild_id: str,
    worker_id: str,
    task_id: str,
) -> None:
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
        # Use "awaiting-review" so the join handler does not replay the task as
        # task-assigned (it only replays "pending" and "working" tasks).
        conn.execute(
            "INSERT OR IGNORE INTO tasks (id, worker_id, guild_pk, description, tool, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, worker_id, guild_pk, "test task", "claude", "awaiting-review", now),
        )
        conn.commit()


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
    test_client, db_path = client
    guild_id, worker_id, task_id = "gwspr1", "w-wspr1", "t-wspr1"
    agent_id = "a-wspr1"

    _insert_guild_worker_task(db_path, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

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
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT pr_url, pr_number, pr_repo, state FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()

    assert row is not None
    pr_url, pr_number, pr_repo, state = row
    assert pr_url == "https://github.com/owner/repo/pull/42"
    assert pr_number == 42
    assert pr_repo == "owner/repo"
    # state stays "awaiting-review" (the .where(state=="working") guard is a no-op here)
    assert state == "awaiting-review"


def test_task_complete_without_pr_url_leaves_pr_url_null(client):
    """task-complete without prUrl must not write anything to pr_url."""
    test_client, db_path = client
    guild_id, worker_id, task_id = "gwspr2", "w-wspr2", "t-wspr2"
    agent_id = "a-wspr2"

    _insert_guild_worker_task(db_path, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

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

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT pr_url, pr_number, pr_repo FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()

    assert row is not None
    assert row[0] is None  # pr_url stays NULL
    assert row[1] is None  # pr_number stays NULL
    assert row[2] is None  # pr_repo stays NULL


# ---------------------------------------------------------------------------
# task-followup-done persists pr_url
# ---------------------------------------------------------------------------


def test_task_followup_done_persists_pr_url(client):
    """task-followup-done with prUrl writes pr_url, pr_number, pr_repo to the task row."""
    test_client, db_path = client
    guild_id, worker_id, task_id = "gwspr3", "w-wspr3", "t-wspr3"
    agent_id = "a-wspr3"

    _insert_guild_worker_task(db_path, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

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

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT pr_url, pr_number, pr_repo, state FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()

    assert row is not None
    pr_url, pr_number, pr_repo, state = row
    assert pr_url == "https://github.com/owner/repo/pull/99"
    assert pr_number == 99
    assert pr_repo == "owner/repo"
    assert state == "awaiting-review"
