"""Tests for SystemSleepMonitor and its hook into WSClient's reconnect loop.

pyobjc isn't installed in this test environment (this suite doesn't run on
macOS), so these tests exercise the no-op stub path plus the WSClient
integration using a fake sleep_monitor stand-in — the same shape the real
monitor exposes (an ``is_sleeping`` property).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from pioneer_worker import ws_client as ws_client_mod
from pioneer_worker.sleep_monitor import SystemSleepMonitor
from pioneer_worker.ws_client import WSClient


def test_stub_is_sleeping_always_false_without_pyobjc():
    """Without pyobjc (the case in this test environment), the monitor must
    be a fully inert no-op: start()/stop() do nothing and is_sleeping stays
    False forever."""
    monitor = SystemSleepMonitor()
    assert monitor.is_sleeping is False

    monitor.start()
    assert monitor.is_sleeping is False

    monitor.stop()
    assert monitor.is_sleeping is False


class _FakeSleepMonitor:
    """Minimal stand-in exposing the same surface WSClient depends on."""

    def __init__(self, *, is_sleeping: bool = False) -> None:
        self.is_sleeping = is_sleeping


async def test_ws_client_skips_connect_attempts_while_sleeping(monkeypatch):
    """connect() must not call websockets.connect while sleep_monitor reports
    is_sleeping — the whole point is not to hammer a dead network link."""
    monkeypatch.setattr(ws_client_mod, "_SLEEP_POLL_INTERVAL", 0.01)
    monitor = _FakeSleepMonitor(is_sleeping=True)
    client = WSClient("ws://example.invalid", sleep_monitor=monitor)

    fake_ws = AsyncMock()
    fake_ws.state = None
    fake_ws.closed = False
    connect_mock = AsyncMock(return_value=fake_ws)
    monkeypatch.setattr(ws_client_mod.websockets, "connect", connect_mock)

    connect_task = asyncio.ensure_future(client.connect())
    await asyncio.sleep(0.05)
    assert connect_mock.await_count == 0, "must not dial while is_sleeping is True"

    monitor.is_sleeping = False
    ws = await asyncio.wait_for(connect_task, timeout=2.0)

    assert ws is fake_ws
    assert connect_mock.await_count == 1


async def test_ws_client_connects_normally_when_not_sleeping(monkeypatch):
    """Baseline: with no sleep_monitor at all, connect() behaves as before —
    dials immediately, no polling."""
    client = WSClient("ws://example.invalid")
    assert client._is_system_sleeping is False

    fake_ws = AsyncMock()
    fake_ws.state = None
    fake_ws.closed = False
    connect_mock = AsyncMock(return_value=fake_ws)
    monkeypatch.setattr(ws_client_mod.websockets, "connect", connect_mock)

    ws = await asyncio.wait_for(client.connect(), timeout=2.0)

    assert ws is fake_ws
    assert connect_mock.await_count == 1
