"""Tests for WSClient's sleep/wake reconnect debounce."""

from __future__ import annotations

from itertools import chain, repeat
from unittest.mock import AsyncMock

from pioneer_worker import ws_client as ws_client_mod
from pioneer_worker.ws_client import WSClient


async def test_ws_client_waits_after_large_clock_gap(monkeypatch):
    """A long event-loop pause likely means laptop sleep; wait before reconnecting."""
    times = chain([0.0, 120.0, 150.0], repeat(150.0))
    monkeypatch.setattr(ws_client_mod.time, "time", lambda: next(times))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(ws_client_mod.asyncio, "sleep", sleep_mock)

    client = WSClient(
        "ws://example.invalid",
        wake_gap_threshold=60.0,
        wake_grace_seconds=0.25,
    )
    fake_ws = AsyncMock()
    fake_ws.state = None
    fake_ws.closed = False
    connect_mock = AsyncMock(return_value=fake_ws)
    monkeypatch.setattr(ws_client_mod.websockets, "connect", connect_mock)

    ws = await client.connect()

    assert ws is fake_ws
    sleep_mock.assert_awaited_once_with(0.25)
    assert connect_mock.await_count == 1


async def test_ws_client_connects_normally_without_clock_gap(monkeypatch):
    times = chain([0.0, 1.0], repeat(1.0))
    monkeypatch.setattr(ws_client_mod.time, "time", lambda: next(times))
    sleep_mock = AsyncMock()
    monkeypatch.setattr(ws_client_mod.asyncio, "sleep", sleep_mock)

    client = WSClient("ws://example.invalid")
    fake_ws = AsyncMock()
    fake_ws.state = None
    fake_ws.closed = False
    connect_mock = AsyncMock(return_value=fake_ws)
    monkeypatch.setattr(ws_client_mod.websockets, "connect", connect_mock)

    ws = await client.connect()

    assert ws is fake_ws
    sleep_mock.assert_not_awaited()
    assert connect_mock.await_count == 1
