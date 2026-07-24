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
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
from _test_config import TEST_DATABASE_URL  # noqa: E402
from helpers import _sync_session, insert_guild, insert_task, insert_worker
from models import (  # noqa: E402
    Agent,
    GithubToken,
    Guild,
    GuildMember,
    Lock,
    Task,
    TaskEvent,
    User,
    UserSession,
)
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlmodel import col  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="function")
def client(_setup_schema):
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
    # Use "awaiting-review" so the join handler does not replay the task as
    # task-assigned (it only replays "pending" and "working" tasks).
    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")
    insert_task(db_url, guild_id, task_id, worker_id=worker_id, state="awaiting-review")


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
    # The join handler replays any pending/working tasks via task-assigned
    # *before* the agent-joined broadcast, so drain those first.
    while True:
        recv = ws.receive_json()
        if recv["type"] == "agent-joined":
            break


def test_agent_state_persists_current_task_id(client):
    """`agent-state` with taskId writes ``current_task_id`` to the agents row."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-1", "w-gas1", "t-gas1", "a-gas1"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs1")
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

            with _sync_session(db_url) as session:
                row = session.execute(
                    select(col(Agent.state), col(Agent.activity), col(Agent.current_task_id)).where(
                        col(Agent.id) == agent_id
                    )
                ).first()

    assert row is not None
    assert row.state == "working"
    assert row.activity == "editing"
    assert row.current_task_id == task_id


def test_agent_state_idle_clears_current_task_id(client):
    """Going idle must null ``current_task_id`` even if taskId is omitted."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-2", "w-gas2", "t-gas2", "a-gas2"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs2")
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

            with _sync_session(db_url) as session:
                row = session.execute(
                    select(col(Agent.state), col(Agent.activity), col(Agent.current_task_id)).where(
                        col(Agent.id) == agent_id
                    )
                ).first()

    assert row is not None
    assert row.state == "idle"
    assert row.activity is None
    assert row.current_task_id is None


def test_agent_state_explicit_task_id_null_clears(client):
    """Explicit ``taskId: null`` in any state clears ``current_task_id``."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-3", "w-gas3", "t-gas3", "a-gas3"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs3")
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

            with _sync_session(db_url) as session:
                current_task_id = session.scalar(
                    select(col(Agent.current_task_id)).where(col(Agent.id) == agent_id)
                )

    assert current_task_id is None


def test_guild_get_returns_current_task_id(client):
    """REST snapshot exposes ``current_task_id`` so a page reload mid-task
    can map the bench to the right slot without waiting for the next WS event."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-4", "w-gas4", "t-gas4", "a-gas4"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    headers = {"Authorization": "Bearer test-token"}
    # Seed the test token & guild membership the way other tests do.

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(User)
            .values(
                id="u-gas4",
                github_id="999004",
                github_login="tester",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing()
        )
        session.execute(
            pg_insert(GithubToken)
            .values(
                github_user_id="u-gas4",
                github_username="tester",
                access_token="gh_tok_fake",
                token_type="bearer",
                scope="repo",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing()
        )
        session.execute(
            pg_insert(UserSession)
            .values(token="test-token", github_user_id="u-gas4", created_at=now)
            .on_conflict_do_update(
                index_elements=["token"],
                set_={"github_user_id": "u-gas4"},
            )
        )
        guild_pk = session.scalar(
            select(col(Guild.id)).where(
                col(Guild.slug) == guild_id, col(Guild.deleted_at).is_(None)
            )
        )
        session.execute(
            pg_insert(GuildMember)
            .values(guild_id=guild_pk, user_id="u-gas4", role="member", created_at=now)
            .on_conflict_do_nothing()
        )
        session.commit()

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs4")
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


def test_agent_idle_releases_task_lock(client):
    """When an agent transitions to idle, the task lock is released and the
    task is moved out of 'working' so a new follow-up can be dispatched."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-5", "w-gas5", "t-gas5", "a-gas5"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    now_dt = datetime.now(UTC)
    future_dt = now_dt + timedelta(hours=1)
    with _sync_session(db_url) as session:
        session.execute(update(Task).where(col(Task.id) == task_id).values(state="working"))
        session.execute(
            pg_insert(Lock)
            .values(
                key=f"task:{task_id}",
                owner="some-lock-holder",
                acquired_at=now_dt,
                expires_at=future_dt,
            )
            .on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "owner": "some-lock-holder",
                    "acquired_at": now_dt,
                    "expires_at": future_dt,
                },
            )
        )
        session.commit()

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs5")
            ws_worker.receive_json()  # drain obs's join broadcast

            # Seed the agent row with current_task_id so handle_agent_state
            # knows which lock to release when the idle signal arrives.
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

            # Now the agent transitions to idle (finished but forgot task-followup-done).
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

    with _sync_session(db_url) as session:
        lock_key = session.scalar(select(col(Lock.key)).where(col(Lock.key) == f"task:{task_id}"))
        task_state = session.scalar(select(col(Task.state)).where(col(Task.id) == task_id))

    assert lock_key is None, "lock should be released when agent goes idle"
    assert task_state == "awaiting-review", (
        "task should move to awaiting-review when its agent goes idle"
    )


def test_error_state_releases_lock_and_drains_followups(client):
    """task-update with state=error releases the lock and drains pending-followup events."""
    test_client, db_url = client
    guild_id, worker_id, task_id, agent_id = "gas-6", "w-gas6", "t-gas6", "a-gas6"
    _insert_guild_worker_task(db_url, guild_id=guild_id, worker_id=worker_id, task_id=task_id)

    now_dt = datetime.now(UTC)
    future_dt = now_dt + timedelta(hours=1)
    with _sync_session(db_url) as session:
        session.execute(update(Task).where(Task.id == task_id).values(state="working"))
        session.execute(
            pg_insert(Lock)
            .values(
                key=f"task:{task_id}",
                owner="some-lock-holder",
                acquired_at=now_dt,
                expires_at=future_dt,
            )
            .on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "owner": "some-lock-holder",
                    "acquired_at": now_dt,
                    "expires_at": future_dt,
                },
            )
        )
        session.add(
            TaskEvent(
                task_id=task_id,
                event_type="pending-followup",
                payload_json='{"instructions": "fix the tests", "preferred_worker_id": null}',
                created_at=now_dt,
            )
        )
        session.commit()

    with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
        _join_ws(ws_worker, agent_id, worker_id)
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
            _join_ws(ws_obs, "a-obs6")
            ws_worker.receive_json()  # drain obs's join broadcast

            ws_worker.send_json(
                {
                    "type": "task-update",
                    "taskId": task_id,
                    "workerId": worker_id,
                    "state": "error",
                }
            )
            ws_obs.receive_json()  # drain the broadcast

    with _sync_session(db_url) as session:
        lock_key = session.scalar(select(Lock.key).where(Lock.key == f"task:{task_id}"))
        task_state = session.scalar(select(Task.state).where(Task.id == task_id))
        remaining_event = session.scalar(
            select(TaskEvent).where(
                TaskEvent.task_id == task_id, TaskEvent.event_type == "pending-followup"
            )
        )

    assert lock_key is None, "lock must be released when task transitions to error"
    assert task_state == "error"
    assert remaining_event is None, "pending-followup events must be drained on error transition"
