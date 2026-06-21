"""WebSocket integration tests for the standalone foreman.

Uses an in-process MockWebSocket to exercise Foreman._run_connection()
without a real backend.  The LLM call (run_foreman_ai) is patched out.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from pioneer_foreman.config import Config
from pioneer_foreman.foreman import Foreman

# ── helpers ───────────────────────────────────────────────────────────────


def _make_config(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test123",
        backend_key="s3cr3t",
        # Large poll interval so the poll loop doesn't fire during tests
        poll_min_interval=3600,
        poll_max_interval=14400,
        **kwargs,
    )


class MockWebSocket:
    """Lightweight WebSocket stand-in.

    Yields predefined messages one by one; blocks on an asyncio.sleep once
    the queue is drained.  The sleep is cancelled when the outer task that
    called _run_connection() is cancelled or when the loop breaks naturally
    (e.g. after receiving foreman-evicted).

    sent — list of decoded dicts that were passed to send().
    """

    def __init__(self, messages: list[dict]) -> None:
        self.sent: list[dict] = []
        self._messages = list(messages)
        self._idx = 0

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        # Yield to the event loop so tasks created by previous messages run
        await asyncio.sleep(0)
        if self._idx < len(self._messages):
            msg = self._messages[self._idx]
            self._idx += 1
            return json.dumps(msg)
        # No more messages — block until cancelled (simulates an idle connection)
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ── WS registration ───────────────────────────────────────────────────────


async def test_ws_sends_join_on_connect():
    """Foreman sends join with agentType=foreman and external=True right after connecting."""
    ws = MockWebSocket([{"type": "foreman-evicted", "reason": "test"}])

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        await foreman._run_connection()

    join_msgs = [m for m in ws.sent if m.get("type") == "join"]
    assert len(join_msgs) == 1, f"Expected exactly one join message, got: {ws.sent}"
    assert join_msgs[0]["agentType"] == "foreman"
    assert join_msgs[0]["external"] is True


async def test_ws_join_sent_before_first_message_processed():
    """join is sent before the foreman starts consuming messages."""
    joined_before_registered = []

    class TrackingWS(MockWebSocket):
        async def __anext__(self) -> str:
            await asyncio.sleep(0)
            if self._idx == 0:
                # Record whether join was sent before we deliver foreman-registered
                joined_before_registered.append(any(m.get("type") == "join" for m in self.sent))
            return await super().__anext__()

    ws = TrackingWS(
        [
            {"type": "foreman-registered", "agentId": "a-001"},
            {"type": "foreman-evicted", "reason": "done"},
        ]
    )

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        foreman._http.get_state.return_value = {"tasks": [], "workers": [], "guild": {}}
        await foreman._run_connection()

    assert joined_before_registered[0] is True, "join should be sent before first message"


async def test_ws_foreman_registered_accepted():
    """Foreman continues running after receiving foreman-registered (no error)."""
    ws = MockWebSocket(
        [
            {"type": "foreman-registered", "agentId": "a-ext01"},
            {"type": "foreman-evicted", "reason": "clean exit"},
        ]
    )

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        foreman._http.get_state.return_value = {"tasks": [], "workers": [], "guild": {}}
        evicted = await foreman._run_connection()

    # Reaching here without exception means foreman-registered was handled
    assert evicted is True


# ── foreman-evicted ───────────────────────────────────────────────────────


async def test_ws_evicted_returns_true():
    """_run_connection returns True when foreman-evicted is received."""
    ws = MockWebSocket([{"type": "foreman-evicted", "reason": "another foreman connected"}])

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        foreman._http.get_state.return_value = {"tasks": [], "workers": [], "guild": {}}
        evicted = await foreman._run_connection()

    assert evicted is True


async def test_ws_clean_disconnect_returns_false():
    """_run_connection returns False when the WS closes without eviction."""

    class ClosingWS(MockWebSocket):
        async def __anext__(self) -> str:
            raise StopAsyncIteration

    ws = ClosingWS([])

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        foreman._http.get_state.return_value = {"tasks": [], "workers": [], "guild": {}}
        evicted = await foreman._run_connection()

    assert evicted is False


# ── foreman-trigger dispatch ──────────────────────────────────────────────


async def test_ws_trigger_dispatches_run_foreman_ai():
    """foreman-trigger causes run_foreman_ai to be called with the right args."""
    trigger = {
        "type": "foreman-trigger",
        "guildId": "test123",
        "humanMessage": "check tasks",
        "userId": "u-abc",
        "event": "task-complete",
    }
    ws = MockWebSocket([trigger, {"type": "foreman-evicted", "reason": "done"}])

    calls: list[dict] = []

    async def fake_run_foreman_ai(guild_id, human_message, *, http, ws_send, config, **kwargs):
        calls.append({"guild_id": guild_id, "human_message": human_message, **kwargs})
        return False

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", fake_run_foreman_ai),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        await foreman._run_connection()
        # Drain any pending tasks
        await asyncio.sleep(0.05)

    assert len(calls) == 1, f"Expected one run_foreman_ai call, got {calls}"
    assert calls[0]["guild_id"] == "test123"
    assert calls[0]["human_message"] == "check tasks"
    assert calls[0].get("user_id") == "u-abc"


async def test_ws_trigger_with_extra_context():
    """extraContext is forwarded to run_foreman_ai."""
    trigger = {
        "type": "foreman-trigger",
        "guildId": "test123",
        "humanMessage": "review PR",
        "extraContext": "PR #42 was opened",
        "event": "pr-opened",
    }
    ws = MockWebSocket([trigger, {"type": "foreman-evicted", "reason": "done"}])

    calls: list[dict] = []

    async def fake_run(*args, extra_context="", **kwargs):
        calls.append(extra_context)
        return False

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", fake_run),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        await foreman._run_connection()
        await asyncio.sleep(0.05)

    assert calls == ["PR #42 was opened"]


async def test_ws_trigger_malformed_ignored():
    """A foreman-trigger missing humanMessage is silently dropped."""
    bad_trigger = {
        "type": "foreman-trigger",
        "guildId": "test123",
        # humanMessage missing
    }
    ws = MockWebSocket([bad_trigger, {"type": "foreman-evicted", "reason": "done"}])

    calls: list = []

    async def fake_run(*args, **kwargs):
        calls.append(1)
        return False

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", fake_run),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        await foreman._run_connection()
        await asyncio.sleep(0.05)

    assert calls == [], "Malformed trigger should not spawn a run"


async def test_ws_second_trigger_queued_when_in_flight():
    """If a run is already in-flight, a second trigger is queued and processed after the first."""
    triggers = [
        {"type": "foreman-trigger", "guildId": "test123", "humanMessage": "first"},
        {"type": "foreman-trigger", "guildId": "test123", "humanMessage": "second"},
        {"type": "foreman-evicted", "reason": "done"},
    ]
    ws = MockWebSocket(triggers)

    call_count = 0
    gate = asyncio.Event()

    async def blocking_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await gate.wait()
        return True

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", blocking_run),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        run_task = asyncio.create_task(foreman._run_connection())
        # Let the first trigger start and the second arrive while first is blocking
        await asyncio.sleep(0.05)
        gate.set()
        await run_task
        await asyncio.sleep(0.05)  # let drain complete

    assert call_count == 2, (
        f"Expected 2 run_foreman_ai calls (first + queued second), got {call_count}"
    )


# ── in-flight flag reset ──────────────────────────────────────────────────


async def test_processing_cleared_after_run():
    """_processing is reset to False after a foreman run (and its queue drain) completes."""
    ws = MockWebSocket(
        [
            {"type": "foreman-trigger", "guildId": "test123", "humanMessage": "do stuff"},
            {"type": "foreman-evicted", "reason": "done"},
        ]
    )

    async def fake_run(*args, **kwargs):
        return False

    foreman = Foreman(_make_config())
    foreman._http = AsyncMock()

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", fake_run),
    ):
        await foreman._run_connection()
        await asyncio.sleep(0.05)  # let the trigger task finish

    assert foreman._processing is False


# ── graceful exit ─────────────────────────────────────────────────────────


async def test_run_cancels_poll_task_on_eviction():
    """poll_task is cancelled and awaited when _run_connection exits."""
    original_create_task = asyncio.create_task

    created_poll_task: list[asyncio.Task] = []

    def patched_create_task(coro, *, name=None):
        task = original_create_task(coro, name=name)
        if name and "poll-loop" in name:
            created_poll_task.append(task)
        return task

    ws = MockWebSocket([{"type": "foreman-evicted", "reason": "cleanup test"}])

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("asyncio.create_task", side_effect=patched_create_task),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        await foreman._run_connection()

    # The poll task should be done (cancelled or finished)
    assert created_poll_task, "Expected a poll-loop task to be created"
    assert created_poll_task[0].done(), "poll task should be done after _run_connection exits"


# ── message queue ─────────────────────────────────────────────────────────


async def test_ws_queued_messages_processed_in_order():
    """Messages received while busy are processed in FIFO order after the turn completes."""
    triggers = [
        {"type": "foreman-trigger", "guildId": "test123", "humanMessage": "first", "event": "e1"},
        {"type": "foreman-trigger", "guildId": "test123", "humanMessage": "second", "event": "e2"},
        {"type": "foreman-trigger", "guildId": "test123", "humanMessage": "third", "event": "e3"},
        {"type": "foreman-evicted", "reason": "done"},
    ]
    ws = MockWebSocket(triggers)

    processed: list[str] = []
    gate = asyncio.Event()

    async def ordered_run(guild_id, human_message, *, http, ws_send, config, **kwargs):
        if human_message == "first":
            await gate.wait()  # block until gate opens; second and third will queue
        processed.append(human_message)
        return False

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", ordered_run),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        run_task = asyncio.create_task(foreman._run_connection())
        await asyncio.sleep(0.05)  # let first trigger start; second and third queue up
        gate.set()
        await run_task
        await asyncio.sleep(0.05)  # let drain complete

    assert processed == ["first", "second", "third"], (
        f"Expected FIFO order ['first', 'second', 'third'], got {processed}"
    )


async def test_ws_periodic_check_discarded_when_busy():
    """Periodic-check triggers are silently discarded (not queued) while a run is in-flight."""
    periodic_msg = (
        "[periodic-check] Automated status poll — 1 non-terminal task(s): t-abc (working). "
        "Check whether any are stalled."
    )
    triggers = [
        {"type": "foreman-trigger", "guildId": "test123", "humanMessage": "real task"},
        {"type": "foreman-trigger", "guildId": "test123", "humanMessage": periodic_msg},
        {"type": "foreman-evicted", "reason": "done"},
    ]
    ws = MockWebSocket(triggers)

    processed: list[str] = []
    gate = asyncio.Event()

    async def tracking_run(guild_id, human_message, *, http, ws_send, config, **kwargs):
        if human_message == "real task":
            await gate.wait()
        processed.append(human_message)
        return False

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", tracking_run),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        run_task = asyncio.create_task(foreman._run_connection())
        await asyncio.sleep(0.05)
        gate.set()
        await run_task
        await asyncio.sleep(0.05)

    assert processed == ["real task"], f"Periodic-check should be discarded; got {processed}"
    assert foreman._message_queue.empty(), "Queue should be empty — periodic-check was not queued"


async def test_ws_queue_full_drops_message():
    """When the message queue is full, the overflow trigger is dropped with a warning."""
    # 1 blocking trigger + 100 that fill the queue + 1 overflow + evicted
    triggers = (
        [{"type": "foreman-trigger", "guildId": "test123", "humanMessage": "first", "event": "e0"}]
        + [
            {
                "type": "foreman-trigger",
                "guildId": "test123",
                "humanMessage": f"msg{i}",
                "event": f"e{i + 1}",
            }
            for i in range(101)  # 100 fill the queue; the 101st is dropped
        ]
        + [{"type": "foreman-evicted", "reason": "done"}]
    )
    ws = MockWebSocket(triggers)

    gate = asyncio.Event()
    processed: list[str] = []

    async def counting_run(guild_id, human_message, *, http, ws_send, config, **kwargs):
        if human_message == "first":
            await gate.wait()
        processed.append(human_message)
        return False

    import logging

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_foreman_ai", counting_run),
    ):
        foreman = Foreman(_make_config())
        foreman._http = AsyncMock()
        run_task = asyncio.create_task(foreman._run_connection())
        await asyncio.sleep(0.1)  # let all triggers arrive while first is blocking
        gate.set()
        await run_task
        await asyncio.sleep(0.1)  # let drain complete

    # "first" + 100 queued = 101 processed; the 101st buffered message was dropped
    assert processed[0] == "first"
    assert len(processed) == 101, (
        f"Expected 101 processed (first + 100 queued), got {len(processed)}"
    )
