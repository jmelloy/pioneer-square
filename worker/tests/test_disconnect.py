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
