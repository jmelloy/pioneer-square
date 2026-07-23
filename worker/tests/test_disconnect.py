"""Tests for the worker graceful-disconnect path.

Verifies that:
  1. _notify_offline() sends a worker-disconnect message to the server.
  2. Worker.run() calls _notify_offline() in its finally block before closing
     the WebSocket, even when the task is cancelled.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pioneer_worker.config import Config
from pioneer_worker.worker import Worker


def _make_cfg(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test-guild",
        worker_id="w-test01",
        max_agents=1,
        pull_interval=3600.0,
        **kwargs,
    )


async def test_notify_offline_sends_worker_disconnect():
    """_notify_offline() must send exactly one worker-disconnect message."""
    worker = Worker(_make_cfg())
    sent: list[dict] = []
    worker._send = AsyncMock(side_effect=lambda p: sent.append(p))

    await worker._notify_offline()

    disconnect_msgs = [m for m in sent if m.get("type") == "worker-disconnect"]
    assert len(disconnect_msgs) == 1, f"Expected one worker-disconnect, got: {sent}"
    assert disconnect_msgs[0]["workerId"] == "w-test01"


async def test_notify_offline_swallows_send_errors():
    """_notify_offline() must not raise even if the WS send fails."""
    worker = Worker(_make_cfg())
    worker._send = AsyncMock(side_effect=OSError("connection closed"))

    # Should complete without raising
    await worker._notify_offline()


async def test_run_sends_disconnect_before_ws_close():
    """run() must send worker-disconnect in the finally block before ws.close()."""
    worker = Worker(_make_cfg())

    sent: list[dict] = []
    close_calls: list[bool] = []

    async def capture_send(payload: dict) -> None:
        sent.append(payload)

    async def capture_close() -> None:
        close_calls.append(True)

    async def hanging_messages():
        """Async generator that blocks until cancelled (simulates idle listener)."""
        await asyncio.sleep(3600)
        yield {}  # Never reached; makes this an async generator

    worker._send = capture_send
    worker._register = AsyncMock()
    worker._fetch_github_token_if_needed = AsyncMock()
    worker._fetch_guild_env_vars = AsyncMock()
    worker._check_gh_auth = AsyncMock()
    worker._check_codex_doctor = AsyncMock()
    worker._check_claude_auth = AsyncMock()
    worker._fetch_pending_tasks = AsyncMock(return_value=[])
    worker.ws.connect = AsyncMock()
    worker.ws.close = capture_close
    worker.ws.messages = hanging_messages

    task = asyncio.create_task(worker.run())
    # Allow run() to reach the asyncio.gather() await
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    disconnect_msgs = [m for m in sent if m.get("type") == "worker-disconnect"]
    assert disconnect_msgs, (
        f"worker-disconnect not found in sent messages: {[m.get('type') for m in sent]}"
    )
    assert disconnect_msgs[0]["workerId"] == "w-test01"
    assert close_calls, "ws.close() was never called"


async def test_on_ws_reconnect_resends_join_and_register():
    """After a WS reconnect, the worker must re-announce itself so the backend
    knows it's online again. Without this the frontend shows the worker offline
    even though the WebSocket is back up."""
    cfg = _make_cfg()
    cfg.max_agents = 2
    worker = Worker(cfg)
    worker._joined = True  # simulate post-auth state
    sent: list[dict] = []
    worker._send = AsyncMock(side_effect=lambda p: sent.append(p))

    await worker._on_ws_reconnect()

    join_msgs = [m for m in sent if m.get("type") == "join"]
    register_msgs = [m for m in sent if m.get("type") == "worker-register"]
    assert len(join_msgs) == 2, f"Expected one join per slot, got {sent}"
    assert len(register_msgs) == 1, f"Expected one worker-register, got {sent}"
    assert {m["agentId"] for m in join_msgs} == {s.agent_id for s in worker.agents}


async def test_join_assigns_distinct_names_per_slot():
    """Each agent slot must get a distinct name so the UI can tell them apart.
    The frontend strips a trailing /N to derive the worker name, so multi-slot
    workers must send "<worker-name>/<slot-index>" per slot."""
    cfg = _make_cfg(worker_name="mybot")
    cfg.max_agents = 3
    worker = Worker(cfg)
    sent: list[dict] = []
    worker._send = AsyncMock(side_effect=lambda p: sent.append(p))

    await worker._join()

    join_msgs = [m for m in sent if m.get("type") == "join"]
    names = [m["agentName"] for m in join_msgs]
    assert len(names) == 3, f"Expected one join per slot, got: {sent}"
    assert len(set(names)) == 3, f"Expected distinct names per slot, got: {names}"


async def test_on_ws_reconnect_skips_join_before_auth():
    """A WS reconnect during the auth window must not register agents early."""
    worker = Worker(_make_cfg())
    # _joined defaults to False — auth not yet complete
    sent: list[dict] = []
    worker._send = AsyncMock(side_effect=lambda p: sent.append(p))

    await worker._on_ws_reconnect()

    assert sent == [], f"No messages should be sent before auth, got: {sent}"


async def test_check_claude_auth_sets_event_from_env():
    """Credentials already in env mark Claude ready synchronously — no login."""
    worker = Worker(_make_cfg())
    worker._run_claude_login = AsyncMock()
    import os

    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "tok-abc"
    try:
        await worker._check_claude_auth()
    finally:
        del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]

    assert worker._claude_authed.is_set()
    worker._run_claude_login.assert_not_awaited()


async def test_check_claude_auth_does_not_block_when_unauthenticated(monkeypatch):
    """With no creds, _check_claude_auth returns immediately and runs login in the
    background — it must not block the join, and _claude_authed stays unset until
    the login actually completes."""
    worker = Worker(_make_cfg())
    worker._send = AsyncMock()
    worker._emit = AsyncMock()
    monkeypatch.setattr(worker, "_claude_is_authenticated", AsyncMock(return_value=False))

    login_started = asyncio.Event()
    login_release = asyncio.Event()

    async def fake_login():
        login_started.set()
        await login_release.wait()  # simulate the 300s human-paste wait

    monkeypatch.setattr(worker, "_run_claude_login", fake_login)
    # Make the backend credential fetch fail fast so we hit the login path.
    monkeypatch.setattr(worker, "_http", AsyncMock(side_effect=OSError("no backend")))

    # Must return promptly despite the login "blocking" indefinitely.
    await asyncio.wait_for(worker._check_claude_auth(), timeout=2.0)

    assert not worker._claude_authed.is_set(), (
        "auth must not be marked ready before login completes"
    )
    await asyncio.wait_for(login_started.wait(), timeout=2.0)  # login runs in background
    login_release.set()
    if worker._claude_login_task:
        await worker._claude_login_task


async def test_on_ws_reconnect_resends_non_idle_agent_state():
    """A reconnect during an active task must restore the slot's actual state
    so the backend doesn't leave the agent stuck at idle."""
    cfg = _make_cfg()
    cfg.max_agents = 2
    worker = Worker(cfg)
    worker._joined = True  # simulate post-auth state
    worker.agents[0].state = "working"
    worker.agents[1].state = "idle"
    sent: list[dict] = []
    worker._send = AsyncMock(side_effect=lambda p: sent.append(p))

    await worker._on_ws_reconnect()

    state_msgs = [m for m in sent if m.get("type") == "agent-state"]
    assert len(state_msgs) == 1, (
        f"Only the non-idle slot should resend agent-state, got: {state_msgs}"
    )
    assert state_msgs[0]["agentId"] == worker.agents[0].agent_id
    assert state_msgs[0]["state"] == "working"


async def test_set_state_tracks_state_on_slot():
    """_set_state must record the current state so reconnect can restore it."""
    worker = Worker(_make_cfg())
    worker._send = AsyncMock()
    slot = worker.agents[0]
    assert slot.state == "idle"

    await worker._set_state("working", slot)
    assert slot.state == "working"

    await worker._set_state("awaiting-review", slot)
    assert slot.state == "awaiting-review"


async def test_worker_ping_replies_with_worker_pong():
    """The worker replies to backend liveness probes with worker-pong."""
    worker = Worker(_make_cfg())
    worker._joined = True
    sent: list[dict] = []
    worker._send = AsyncMock(side_effect=lambda p: sent.append(p))

    async def messages():
        yield {"type": "worker-ping", "workerId": "w-test01", "timestamp": "now"}
        await asyncio.sleep(3600)

    worker.ws.messages = messages

    task = asyncio.create_task(worker._listen())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pongs = [m for m in sent if m.get("type") == "worker-pong"]
    assert len(pongs) == 1, f"Expected one worker-pong, got: {sent}"
    assert pongs[0]["workerId"] == "w-test01"
    assert "timestamp" in pongs[0]
