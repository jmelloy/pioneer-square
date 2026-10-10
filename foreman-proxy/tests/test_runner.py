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


async def test_bedrock_proxy_assumes_its_role_and_keys_the_cache_on_it(monkeypatch):
    import backend.foreman.bedrock_role as bedrock_role

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    runner._anthropic_clients.clear()
    made = []

    def fake_make(*, role_arn, region):
        made.append((role_arn, region))
        return object()

    monkeypatch.setattr(bedrock_role, "make_role_anthropic_bedrock", fake_make)
    cfg = Config(
        backend_url="ws://x:1",
        guild_id="g",
        provider="bedrock",
        bedrock_model="us.anthropic.claude-sonnet-4-6",
        aws_region="us-west-2",
    )

    monkeypatch.setenv("BEDROCK_ROLE_ARN", "arn:aws:iam::123456789012:role/a")
    first = runner._get_anthropic_client(cfg)
    assert runner._get_anthropic_client(cfg) is first
    monkeypatch.setenv("BEDROCK_ROLE_ARN", "arn:aws:iam::210987654321:role/b")
    second = runner._get_anthropic_client(cfg)

    assert second is not first
    assert made == [
        ("arn:aws:iam::123456789012:role/a", "us-west-2"),
        ("arn:aws:iam::210987654321:role/b", "us-west-2"),
    ]
    runner._anthropic_clients.clear()


async def test_run_api_request_openrouter_uses_openai_wire_format(monkeypatch):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "model": "anthropic/claude-sonnet-4.6",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    base = "https://openrouter.ai/api/v1"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(runner._http_clients, (base, "or-key"), client)
    cfg = Config(
        backend_url="ws://x:1",
        guild_id="g",
        provider="openrouter",
        model="anthropic/claude-sonnet-4.6",
        api_key="or-key",
        openai_base_url=base,
    )
    result = await runner.run_api_request(
        {"model": "m", "maxTokens": 10, "messages": [{"role": "user", "content": "hi"}]}, cfg
    )
    assert captured["url"] == f"{base}/chat/completions"
    assert captured["auth"] == "Bearer or-key"
    assert result["provider"] == "openrouter"
    assert result["model"] == "anthropic/claude-sonnet-4.6"
