"""Unit tests for the Discord Gateway websocket client (Phase 4).

No real Discord connection is made — ``websockets.connect`` is patched with a
``FakeGatewayWebSocket`` that feeds pre-scripted frames and records sent
frames. No DB is used — ``_is_channel_wired`` is patched directly for the
MESSAGE_CREATE filtering tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord import gateway  # noqa: E402

_CLOSE = object()


class FakeGatewayWebSocket:
    """Minimal async-context-manager + async-iterator fake of a Gateway connection."""

    def __init__(self, frames):
        self._frames: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self._frames.put_nowait(frame)
        self.sent: list[dict] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def recv(self):
        frame = await self._frames.get()
        if frame is _CLOSE:
            raise StopAsyncIteration
        return frame

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self._frames.get()
        if frame is _CLOSE:
            raise StopAsyncIteration
        return frame

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = True
        await self._frames.put(_CLOSE)


def _hello(interval_ms=41250):
    return json.dumps({"op": gateway._OP_HELLO, "d": {"heartbeat_interval": interval_ms}})


def _dispatch(event_type, data, seq=1):
    return json.dumps({"op": gateway._OP_DISPATCH, "t": event_type, "s": seq, "d": data})


async def _run_briefly(client, ws):
    """Run client.run() against ws for a short window, then cancel it."""
    with patch("discord.gateway.websockets.connect", return_value=ws):
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# IDENTIFY / RESUME handshake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identify_sent_after_hello_with_no_prior_session():
    ws = FakeGatewayWebSocket([_hello(), _CLOSE])
    client = gateway.GatewayClient("test-token")
    await _run_briefly(client, ws)

    identify = [m for m in ws.sent if m["op"] == gateway._OP_IDENTIFY]
    assert len(identify) == 1
    assert identify[0]["d"]["token"] == "test-token"
    assert identify[0]["d"]["intents"] == 33283


@pytest.mark.asyncio
async def test_resume_sent_when_session_already_known():
    ws = FakeGatewayWebSocket([_hello(), _CLOSE])
    client = gateway.GatewayClient("test-token")
    client._session_id = "sess-123"
    client._seq = 42
    await _run_briefly(client, ws)

    resume = [m for m in ws.sent if m["op"] == gateway._OP_RESUME]
    assert len(resume) == 1
    assert resume[0]["d"] == {"token": "test-token", "session_id": "sess-123", "seq": 42}
    assert [m for m in ws.sent if m["op"] == gateway._OP_IDENTIFY] == []


@pytest.mark.asyncio
async def test_ready_stores_session_id_and_resume_url():
    ready = _dispatch(
        "READY", {"session_id": "sess-abc", "resume_gateway_url": "wss://resume.example/"}
    )
    ws = FakeGatewayWebSocket([_hello(), ready, _CLOSE])
    client = gateway.GatewayClient("test-token")
    await _run_briefly(client, ws)

    assert client._session_id == "sess-abc"
    assert client._resume_url == "wss://resume.example/"
    assert client._seq == 1


@pytest.mark.asyncio
async def test_heartbeat_ack_is_tracked():
    ws = FakeGatewayWebSocket([_hello(), json.dumps({"op": gateway._OP_HEARTBEAT_ACK}), _CLOSE])
    client = gateway.GatewayClient("test-token")
    await _run_briefly(client, ws)

    assert client._heartbeat_acked is True


@pytest.mark.asyncio
async def test_invalid_session_not_resumable_clears_state_and_reidentifies():
    """Exercises _handle_frame directly — patching asyncio.sleep globally would
    also stall the wall-clock wait in _run_briefly, so the full run() loop
    isn't used here."""
    ws = FakeGatewayWebSocket([])
    client = gateway.GatewayClient("test-token")
    client._session_id = "stale-session"
    client._seq = 7

    with patch("discord.gateway.asyncio.sleep", new=AsyncMock()):
        await client._handle_frame(ws, json.dumps({"op": gateway._OP_INVALID_SESSION, "d": False}))

    assert client._session_id is None
    assert client._seq is None
    identify = [m for m in ws.sent if m["op"] == gateway._OP_IDENTIFY]
    assert len(identify) == 1


@pytest.mark.asyncio
async def test_invalid_session_resumable_sends_resume_and_keeps_state():
    ws = FakeGatewayWebSocket([])
    client = gateway.GatewayClient("test-token")
    client._session_id = "sess-keep"
    client._seq = 9

    with patch("discord.gateway.asyncio.sleep", new=AsyncMock()):
        await client._handle_frame(ws, json.dumps({"op": gateway._OP_INVALID_SESSION, "d": True}))

    resume = [m for m in ws.sent if m["op"] == gateway._OP_RESUME]
    assert len(resume) == 1
    assert client._session_id == "sess-keep"


# ---------------------------------------------------------------------------
# Reconnect backoff (regression: connect-then-immediately-closed loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unhealthy_connection_backs_off():
    """A connection that opens but never reaches READY must go through the
    backoff branch — not reconnect immediately every cycle (the bug that opened
    1000+ connections and got the bot token reset)."""
    attempts: list[int] = []

    def fake_backoff(attempt, *a, **k):
        attempts.append(attempt)
        raise asyncio.CancelledError  # stop the loop after the first backoff

    # Socket opens, HELLO arrives, then closes before READY — the failure mode.
    def fresh_ws(*_a, **_kw):
        return FakeGatewayWebSocket([_hello(), _CLOSE])

    client = gateway.GatewayClient("test-token")
    with (
        patch("discord.gateway.websockets.connect", side_effect=fresh_ws),
        patch("discord.gateway._backoff_delay", side_effect=fake_backoff),
    ):
        with pytest.raises(asyncio.CancelledError):
            await client.run()

    assert client._healthy is False
    assert attempts == [0]  # entered the backoff branch on the first failure


@pytest.mark.asyncio
async def test_healthy_connection_does_not_back_off():
    """Once READY is seen, a clean close (e.g. server RECONNECT) reconnects
    promptly without ever computing a backoff delay."""
    ready = _dispatch("READY", {"session_id": "s", "resume_gateway_url": "wss://r/"})

    def fresh_ws(*_a, **_kw):
        return FakeGatewayWebSocket([_hello(), ready, _CLOSE])

    client = gateway.GatewayClient("test-token")
    with (
        patch("discord.gateway.websockets.connect", side_effect=fresh_ws),
        patch("discord.gateway._backoff_delay") as backoff,
    ):
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert client._healthy is True
    backoff.assert_not_called()  # never hit the unhealthy backoff branch


# ---------------------------------------------------------------------------
# MESSAGE_CREATE filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_create_filters_bot_author():
    client = gateway.GatewayClient("token", queue=asyncio.Queue())
    message = {"author": {"bot": True}, "guild_id": "g1", "channel_id": "c1"}
    with patch("discord.gateway._is_channel_wired", new=AsyncMock(return_value=True)):
        await client._handle_message_create(message)
    assert client._queue.empty()


@pytest.mark.asyncio
async def test_message_create_filters_dm_with_no_guild_id():
    client = gateway.GatewayClient("token", queue=asyncio.Queue())
    message = {"author": {"bot": False}, "channel_id": "c1"}
    with patch("discord.gateway._is_channel_wired", new=AsyncMock(return_value=True)):
        await client._handle_message_create(message)
    assert client._queue.empty()


@pytest.mark.asyncio
async def test_message_create_filters_unwired_channel():
    client = gateway.GatewayClient("token", queue=asyncio.Queue())
    message = {"author": {"bot": False}, "guild_id": "g1", "channel_id": "c-unwired"}
    with patch("discord.gateway._is_channel_wired", new=AsyncMock(return_value=False)):
        await client._handle_message_create(message)
    assert client._queue.empty()


@pytest.mark.asyncio
async def test_message_create_queued_when_wired():
    q: asyncio.Queue = asyncio.Queue()
    client = gateway.GatewayClient("token", queue=q)
    message = {
        "author": {"bot": False},
        "guild_id": "g1",
        "channel_id": "c-wired",
        "content": "hi",
    }
    with patch("discord.gateway._is_channel_wired", new=AsyncMock(return_value=True)):
        await client._handle_message_create(message)
    assert q.qsize() == 1
    queued = await q.get()
    assert queued["content"] == "hi"


@pytest.mark.asyncio
async def test_message_create_dispatch_reaches_queue_end_to_end():
    """A full MESSAGE_CREATE dispatch frame reaches the queue for a wired channel."""
    q: asyncio.Queue = asyncio.Queue()
    message = {
        "author": {"bot": False, "id": "u1"},
        "guild_id": "g1",
        "channel_id": "c-wired",
        "content": "hello",
    }
    frame = _dispatch("MESSAGE_CREATE", message)
    ws = FakeGatewayWebSocket([_hello(), frame, _CLOSE])
    client = gateway.GatewayClient("test-token", queue=q)

    with patch("discord.gateway._is_channel_wired", new=AsyncMock(return_value=True)):
        await _run_briefly(client, ws)

    assert q.qsize() == 1
    assert (await q.get())["content"] == "hello"


# ---------------------------------------------------------------------------
# GUILD_MEMBER_ADD -> welcome DM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guild_member_add_sends_welcome_dm():
    client = gateway.GatewayClient("token")
    member = {"user": {"id": "u1", "username": "newperson", "bot": False}}
    with patch("discord.gateway.send_welcome_dm", new=AsyncMock()) as mock_send:
        await client._handle_guild_member_add(member)
    mock_send.assert_awaited_once_with("u1", "newperson")


@pytest.mark.asyncio
async def test_guild_member_add_prefers_global_name():
    client = gateway.GatewayClient("token")
    member = {
        "user": {"id": "u1", "username": "newperson", "global_name": "New Person", "bot": False}
    }
    with patch("discord.gateway.send_welcome_dm", new=AsyncMock()) as mock_send:
        await client._handle_guild_member_add(member)
    mock_send.assert_awaited_once_with("u1", "New Person")


@pytest.mark.asyncio
async def test_guild_member_add_skips_bot_accounts():
    client = gateway.GatewayClient("token")
    member = {"user": {"id": "u1", "username": "some-bot", "bot": True}}
    with patch("discord.gateway.send_welcome_dm", new=AsyncMock()) as mock_send:
        await client._handle_guild_member_add(member)
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_guild_member_add_skips_missing_user_id():
    client = gateway.GatewayClient("token")
    with patch("discord.gateway.send_welcome_dm", new=AsyncMock()) as mock_send:
        await client._handle_guild_member_add({"user": {}})
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_guild_member_add_dispatch_end_to_end():
    """A full GUILD_MEMBER_ADD dispatch frame triggers the welcome DM."""
    member = {"user": {"id": "u1", "username": "newperson", "bot": False}}
    frame = _dispatch("GUILD_MEMBER_ADD", member)
    ws = FakeGatewayWebSocket([_hello(), frame, _CLOSE])
    client = gateway.GatewayClient("test-token")

    with patch("discord.gateway.send_welcome_dm", new=AsyncMock()) as mock_send:
        await _run_briefly(client, ws)

    mock_send.assert_awaited_once_with("u1", "newperson")


# ---------------------------------------------------------------------------
# _is_channel_wired TTL cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_channel_wired_caches_positive_result():
    with patch("discord.gateway._query_channel_wired", new=AsyncMock(return_value=True)) as query:
        assert await gateway._is_channel_wired("c-wired") is True
        assert await gateway._is_channel_wired("c-wired") is True
    query.assert_awaited_once_with("c-wired")


@pytest.mark.asyncio
async def test_is_channel_wired_requeries_after_ttl_expires():
    with patch("discord.gateway._query_channel_wired", new=AsyncMock(return_value=False)) as query:
        assert await gateway._is_channel_wired("c-unwired") is False
        gateway._channel_wired_cache["c-unwired"] = (False, 0.0)  # force expiry
        assert await gateway._is_channel_wired("c-unwired") is False
    assert query.await_count == 2


# ---------------------------------------------------------------------------
# start_gateway / stop_gateway env gating
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_gateway_task():
    gateway._gateway_task = None
    gateway._channel_wired_cache.clear()
    yield
    gateway._gateway_task = None
    gateway._channel_wired_cache.clear()


def test_start_gateway_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("DISCORD_GATEWAY_ENABLED", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    assert gateway.start_gateway() is None


def test_start_gateway_noop_without_bot_token(monkeypatch):
    monkeypatch.setenv("DISCORD_GATEWAY_ENABLED", "true")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert gateway.start_gateway() is None


@pytest.mark.asyncio
async def test_start_gateway_spawns_task_and_stop_gateway_cancels_it(monkeypatch):
    monkeypatch.setenv("DISCORD_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    ws = FakeGatewayWebSocket([])  # never yields HELLO — recv() blocks until cancelled

    with patch("discord.gateway.websockets.connect", return_value=ws):
        task = gateway.start_gateway()
        assert task is not None
        assert gateway.start_gateway() is task  # idempotent while already running

        await gateway.stop_gateway()

    assert gateway._gateway_task is None
