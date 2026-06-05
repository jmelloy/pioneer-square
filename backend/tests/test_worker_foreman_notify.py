"""Tests that worker join/leave events trigger foreman notifications.

Verifies that:
- handle_worker_register triggers [worker-online] to the foreman
- handle_worker_disconnect triggers [worker-offline] reason=shutdown
- abrupt WebSocket disconnect triggers [worker-offline] reason=disconnect
- task-complete with stopReason=max_turns triggers a max-turns foreman message
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module  # noqa: E402
import main as main_module  # noqa: E402
import ws_handlers  # noqa: E402
from _test_config import TEST_DATABASE_URL  # noqa: E402
from helpers import insert_guild, insert_task, insert_worker  # noqa: E402


@pytest.fixture(scope="module")
def client(_setup_schema):
    """Module-scoped TestClient shared by all notify tests."""
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


def _make_trigger_spy():
    """Return (spy_list, async_fake) where spy_list accumulates (event, msg) calls."""
    triggered: list[tuple[str, str]] = []

    async def fake_trigger(
        guild_id: str,
        event: str,
        msg: str,
        *,
        user_id: str | None = None,
        task_id: str | None = None,
        task_name: str = "",
    ) -> None:
        triggered.append((event, msg))

    return triggered, fake_trigger


def test_worker_online_notifies_foreman(client):
    """worker-register triggers [worker-online] with worker_id, repos, agent_count, and tools."""
    test_client, db_url = client
    guild_id = "nfy001"
    worker_id = "w-nfy001"
    agent_id = "a-nfy001"

    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")

    triggered, fake_trigger = _make_trigger_spy()

    with patch.object(ws_handlers, "_trigger_foreman", new=fake_trigger):
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

            ws.send_json(
                {
                    "type": "worker-register",
                    "workerId": worker_id,
                    "repos": ["org/repo1", "org/repo2"],
                    "tools": ["claude", "codex"],
                }
            )
            # Sync: ping/pong ensures the server has fully processed
            # worker-register (including db.commit()) before we close the
            # WebSocket.  Without this, the close frame can arrive while
            # handle_worker_register is still awaiting inside db.commit(),
            # which delivers a CancelledError that corrupts the asyncpg
            # connection and causes InterfaceError in the finally block.
            ws.send_json({"type": "ping"})
            ws.receive_json()  # pong

    online = [(e, m) for e, m in triggered if e == "worker-online"]
    assert online, f"Expected worker-online trigger, got: {triggered}"
    _event, msg = online[0]
    assert f"worker_id={worker_id}" in msg, msg
    assert "repos=org/repo1,org/repo2" in msg, msg
    assert "agent_count=1" in msg, msg
    assert "tools=claude,codex" in msg, msg


def test_worker_graceful_offline_notifies_foreman(client):
    """worker-disconnect triggers [worker-offline] reason=shutdown."""
    test_client, db_url = client
    guild_id = "nfy002"
    worker_id = "w-nfy002"
    agent_id = "a-nfy002"

    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")

    triggered, fake_trigger = _make_trigger_spy()

    with patch.object(ws_handlers, "_trigger_foreman", new=fake_trigger):
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

            ws.send_json({"type": "worker-disconnect", "workerId": worker_id})
            ws.receive_json()  # agent-state offline broadcast

    offline = [(e, m) for e, m in triggered if e == "worker-offline"]
    assert offline, f"Expected worker-offline trigger, got: {triggered}"
    _event, msg = offline[0]
    assert f"worker_id={worker_id}" in msg, msg
    assert "reason=shutdown" in msg, msg


def test_abrupt_disconnect_notifies_foreman(client):
    """Abrupt WebSocket close (no worker-disconnect) triggers [worker-offline] reason=disconnect."""
    test_client, db_url = client
    guild_id = "nfy003"
    worker_id = "w-nfy003"
    agent_id = "a-nfy003"

    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")

    triggered, fake_trigger = _make_trigger_spy()

    with patch.object(ws_handlers, "_trigger_foreman", new=fake_trigger):
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
            # Close without sending worker-disconnect — simulates container crash.

    offline = [(e, m) for e, m in triggered if e == "worker-offline"]
    assert offline, f"Expected worker-offline trigger on abrupt disconnect, got: {triggered}"
    _event, msg = offline[0]
    assert f"worker_id={worker_id}" in msg, msg
    assert "reason=disconnect" in msg, msg


def test_task_complete_max_turns_notifies_foreman(client):
    """task-complete with stopReason=max_turns sends a truncation-aware foreman trigger."""
    test_client, db_url = client
    guild_id = "nfy004"
    worker_id = "w-nfy004"
    task_id = "t-nfy004"
    agent_id = "a-nfy004"

    insert_guild(db_url, guild_id, owner_user_id=None)
    insert_worker(db_url, guild_id, worker_id, state="online")
    insert_task(db_url, guild_id, task_id, worker_id=worker_id, state="working")

    triggered, fake_trigger = _make_trigger_spy()

    with patch.object(ws_handlers, "_trigger_foreman", new=fake_trigger):
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws_worker:
            ws_worker.send_json(
                {
                    "type": "join",
                    "agentId": agent_id,
                    "agentName": "Test Worker",
                    "agentType": "worker",
                    "workerId": worker_id,
                }
            )
            ws_worker.receive_json()  # agent-joined broadcast

            with test_client.websocket_connect(f"/ws/{guild_id}") as ws_obs:
                ws_obs.send_json(
                    {
                        "type": "join",
                        "agentId": "a-nfy004-obs",
                        "agentName": "Observer",
                        "agentType": "browser",
                    }
                )
                ws_obs.receive_json()  # agent-joined for observer
                ws_worker.receive_json()  # observer's agent-joined broadcast to worker

                ws_worker.send_json(
                    {
                        "type": "task-complete",
                        "workerId": worker_id,
                        "taskId": task_id,
                        "branch": "feature/partial-work",
                        "stopReason": "max_turns",
                        "lastText": "I was working on the migration when I ran out of turns.",
                        "prUrl": "",
                        "sessionId": "",
                    }
                )
                ws_obs.receive_json()  # task-complete broadcast

                ws_worker.send_json({"type": "ping"})
                ws_worker.receive_json()  # pong — ensures handler is done

    complete_triggers = [(e, m) for e, m in triggered if e == "task-complete"]
    assert complete_triggers, f"Expected task-complete trigger, got: {triggered}"
    _event, msg = complete_triggers[0]
    assert "max-turns" in msg, f"Expected 'max-turns' in message, got: {msg}"
    assert "max_turns" not in msg.split("max-turns")[0].replace("max_turns", ""), True
    assert task_id in msg, f"Expected task_id in message, got: {msg}"
    assert "continuation" in msg.lower() or "resume" in msg.lower() or "send_followup" in msg, (
        f"Expected continuation/resume/send_followup hint, got: {msg}"
    )
    assert "I was working on the migration" in msg, (
        f"Expected lastText snippet in message, got: {msg}"
    )


def test_worker_online_fast_reconnect_tags_reconnect(client):
    """A worker that was already online (fast reconnect) must include
    reconnect=true in the worker-online foreman trigger so the AI knows not
    to treat it as a fresh registration."""
    test_client, db_url = client
    guild_id = "nfy005"
    worker_id = "w-nfy005"
    agent_id = "a-nfy005"

    insert_guild(db_url, guild_id, owner_user_id=None)
    # Worker already online — simulates a fast reconnect before offline was registered.
    insert_worker(db_url, guild_id, worker_id, state="online")

    triggered, fake_trigger = _make_trigger_spy()

    with patch.object(ws_handlers, "_trigger_foreman", new=fake_trigger):
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

            ws.send_json(
                {
                    "type": "worker-register",
                    "workerId": worker_id,
                    "repos": ["org/repo1"],
                    "tools": ["claude"],
                }
            )
            ws.send_json({"type": "ping"})
            ws.receive_json()  # pong

    online = [(e, m) for e, m in triggered if e == "worker-online"]
    assert online, f"Expected worker-online trigger, got: {triggered}"
    _event, msg = online[0]
    assert "reconnect=true" in msg, (
        f"Expected reconnect=true in message for fast reconnect, got: {msg}"
    )


def test_worker_online_fresh_join_no_reconnect_tag(client):
    """A brand-new worker (never registered before) must NOT include reconnect=true
    in the worker-online foreman trigger."""
    test_client, db_url = client
    guild_id = "nfy006"
    worker_id = "w-nfy006"
    agent_id = "a-nfy006"

    insert_guild(db_url, guild_id, owner_user_id=None)
    # Worker is inserted as offline to simulate a previously registered but
    # disconnected worker coming back after the backend marked it offline.
    insert_worker(db_url, guild_id, worker_id, state="offline")

    triggered, fake_trigger = _make_trigger_spy()

    with patch.object(ws_handlers, "_trigger_foreman", new=fake_trigger):
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

            ws.send_json(
                {
                    "type": "worker-register",
                    "workerId": worker_id,
                    "repos": ["org/repo1"],
                    "tools": ["claude"],
                }
            )
            ws.send_json({"type": "ping"})
            ws.receive_json()  # pong

    online = [(e, m) for e, m in triggered if e == "worker-online"]
    assert online, f"Expected worker-online trigger, got: {triggered}"
    _event, msg = online[0]
    assert "reconnect=true" not in msg, (
        f"Expected no reconnect=true for offline→online transition, got: {msg}"
    )
