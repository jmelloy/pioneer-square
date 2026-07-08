"""WebSocket integration tests for the standalone Foreman API proxy."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from pioneer_foreman.config import Config
from pioneer_foreman.foreman import Foreman


def _make_config(**kwargs) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="test123",
        **kwargs,
    )


class MockWebSocket:
    """Lightweight WebSocket stand-in for Foreman._run_connection()."""

    def __init__(self, messages: list[dict]) -> None:
        self.sent: list[dict] = []
        self._messages = list(messages)
        self._idx = 0

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)
        if self._idx < len(self._messages):
            msg = self._messages[self._idx]
            self._idx += 1
            return json.dumps(msg)
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def test_ws_sends_join_on_connect():
    ws = MockWebSocket([{"type": "foreman-evicted", "reason": "test"}])

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        await Foreman(_make_config())._run_connection()

    join_msgs = [m for m in ws.sent if m.get("type") == "join"]
    assert len(join_msgs) == 1
    assert join_msgs[0]["agentType"] == "foreman"
    assert join_msgs[0]["agentName"] == "Foreman API Proxy"
    assert join_msgs[0]["external"] is True


async def test_ws_join_sent_before_first_message_processed():
    joined_before_registered = []

    class TrackingWS(MockWebSocket):
        async def __anext__(self) -> str:
            await asyncio.sleep(0)
            if self._idx == 0:
                joined_before_registered.append(any(m.get("type") == "join" for m in self.sent))
            return await super().__anext__()

    ws = TrackingWS(
        [
            {"type": "foreman-registered", "agentId": "a-001"},
            {"type": "foreman-evicted", "reason": "done"},
        ]
    )

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        await Foreman(_make_config())._run_connection()

    assert joined_before_registered[0] is True


async def test_ws_evicted_returns_true():
    ws = MockWebSocket([{"type": "foreman-evicted", "reason": "another proxy connected"}])

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        evicted = await Foreman(_make_config())._run_connection()

    assert evicted is True


async def test_ws_clean_disconnect_returns_false():
    class ClosingWS(MockWebSocket):
        async def __anext__(self) -> str:
            raise StopAsyncIteration

    ws = ClosingWS([])

    with patch("pioneer_foreman.foreman.websockets.connect", return_value=ws):
        evicted = await Foreman(_make_config())._run_connection()

    assert evicted is False


async def test_ws_api_request_sends_response():
    request = {
        "type": "foreman-api-request",
        "requestId": "req-1",
        "guildId": "test123",
        "model": "backend-model",
        "maxTokens": 1024,
        "system": [],
        "messages": [],
        "tools": [],
    }
    ws = MockWebSocket([request, {"type": "foreman-evicted", "reason": "done"}])
    result = {
        "response": {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "model": "proxy-model",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        "apiRequestId": "api-1",
        "provider": "anthropic",
        "model": "proxy-model",
    }

    async def fake_run_api_request(data, config):
        assert data["requestId"] == "req-1"
        return result

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_api_request", fake_run_api_request),
    ):
        await Foreman(_make_config())._run_connection()
        await asyncio.sleep(0)

    responses = [m for m in ws.sent if m.get("type") == "foreman-api-response"]
    assert responses == [
        {
            "type": "foreman-api-response",
            "requestId": "req-1",
            "guildId": "test123",
            "ok": True,
            **result,
        }
    ]


async def test_ws_api_request_error_sends_error_response():
    request = {
        "type": "foreman-api-request",
        "requestId": "req-err",
        "guildId": "test123",
        "model": "backend-model",
        "system": [],
        "messages": [],
        "tools": [],
    }
    ws = MockWebSocket([request, {"type": "foreman-evicted", "reason": "done"}])

    async def fake_run_api_request(data, config):
        raise RuntimeError("provider failed")

    with (
        patch("pioneer_foreman.foreman.websockets.connect", return_value=ws),
        patch("pioneer_foreman.foreman.run_api_request", fake_run_api_request),
    ):
        await Foreman(_make_config())._run_connection()
        await asyncio.sleep(0)

    responses = [m for m in ws.sent if m.get("type") == "foreman-api-response"]
    assert len(responses) == 1
    assert responses[0]["requestId"] == "req-err"
    assert responses[0]["ok"] is False
    assert "provider failed" in responses[0]["error"]
