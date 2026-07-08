"""Tests for backend-side external Foreman API proxy coordination."""

from __future__ import annotations

import pytest


async def test_call_foreman_api_proxy_roundtrip(monkeypatch):
    import foreman.proxy as proxy

    class FakeWS:
        pass

    ws = FakeWS()
    proxy.foreman_connections["g1"] = ws
    sent = {}

    async def fake_send_ws_message(target_ws, msg):
        assert target_ws is ws
        sent["msg"] = msg
        proxy.resolve_foreman_api_response(
            {
                "type": "foreman-api-response",
                "requestId": msg.requestId,
                "guildId": "g1",
                "ok": True,
                "response": {
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
                "apiRequestId": "api-1",
                "provider": "anthropic",
                "model": "claude",
            }
        )

    monkeypatch.setattr(proxy, "send_ws_message", fake_send_ws_message)
    try:
        result = await proxy.call_foreman_api_proxy(
            "g1",
            model="claude",
            max_tokens=1024,
            system=[],
            messages=[],
            tools=[],
        )
    finally:
        proxy.foreman_connections.pop("g1", None)

    assert sent["msg"].type == "foreman-api-request"
    assert sent["msg"].model == "claude"
    assert result["apiRequestId"] == "api-1"
    assert result["response"]["content"] == [{"type": "text", "text": "ok"}]


async def test_call_foreman_api_proxy_error_response_raises(monkeypatch):
    import foreman.proxy as proxy

    class FakeWS:
        pass

    ws = FakeWS()
    proxy.foreman_connections["g1"] = ws

    async def fake_send_ws_message(target_ws, msg):
        proxy.resolve_foreman_api_response(
            {
                "type": "foreman-api-response",
                "requestId": msg.requestId,
                "guildId": "g1",
                "ok": False,
                "error": "provider failed",
            }
        )

    monkeypatch.setattr(proxy, "send_ws_message", fake_send_ws_message)
    try:
        with pytest.raises(proxy.ForemanProxyError, match="provider failed"):
            await proxy.call_foreman_api_proxy(
                "g1",
                model="claude",
                max_tokens=1024,
                system=[],
                messages=[],
                tools=[],
            )
    finally:
        proxy.foreman_connections.pop("g1", None)
