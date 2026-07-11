"""Tests for the standalone proxy LLM request executor.

The OpenAI-compatible request/response translation itself lives in and is
tested by backend/tests/test_foreman_llm.py (shared with the embedded
foreman). These tests cover only what's local to the proxy: dispatching to the
right provider and wiring config into the shared helpers.
"""

from __future__ import annotations

import json

import httpx
import pioneer_foreman.runner as runner
from pioneer_foreman.config import Config
from pioneer_foreman.runner import run_api_request


async def test_run_api_request_openai_posts_chat_completion(monkeypatch):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            headers={"x-request-id": "req-openai"},
            json={
                "id": "chatcmpl-2",
                "model": "llama3.1",
                "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(runner._http_clients, ("http://ollama.test/v1", "key"), client)

    cfg = Config(
        backend_url="ws://x:1",
        guild_id="g",
        provider="openai",
        model="llama3.1",
        openai_base_url="http://ollama.test/v1",
        api_key="key",
    )
    result = await run_api_request(
        {
            "model": "backend-model",
            "maxTokens": 32,
            "system": [{"type": "text", "text": "System"}],
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [],
        },
        cfg,
    )

    assert captured["url"] == "http://ollama.test/v1/chat/completions"
    assert captured["json"]["model"] == "llama3.1"
    assert captured["headers"]["authorization"] == "Bearer key"
    assert result["apiRequestId"] == "req-openai"
    assert result["provider"] == "openai"
    assert result["response"]["content"] == [{"type": "text", "text": "done"}]

    await client.aclose()


async def test_run_api_request_unsupported_provider_raises():
    cfg = Config(backend_url="ws://x:1", guild_id="g", provider="unsupported")

    try:
        await run_api_request({"model": "m"}, cfg)
    except ValueError as exc:
        assert "unsupported" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError for an unsupported provider")
