"""Tests for the standalone proxy LLM request executor."""

from __future__ import annotations

import json

import httpx
import pioneer_foreman.runner as runner
from pioneer_foreman.config import Config
from pioneer_foreman.runner import (
    _normalise_openai_response,
    _openai_messages,
    _openai_tools,
    run_api_request,
)


def test_openai_tools_wrap_anthropic_schema():
    tools = [
        {
            "name": "assign_task",
            "description": "Assign a task.",
            "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}},
        }
    ]

    assert _openai_tools(tools) == [
        {
            "type": "function",
            "function": {
                "name": "assign_task",
                "description": "Assign a task.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                },
            },
        }
    ]


def test_openai_messages_convert_tool_exchange():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Create it"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll do that."},
                {
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "create_task",
                    "input": {"name": "Build"},
                },
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu-1", "content": "created"}],
        },
    ]

    converted = _openai_messages([{"type": "text", "text": "System"}], messages)

    assert converted == [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Create it"},
        {
            "role": "assistant",
            "content": "I'll do that.",
            "tool_calls": [
                {
                    "id": "toolu-1",
                    "type": "function",
                    "function": {"name": "create_task", "arguments": '{"name": "Build"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu-1", "content": "created"},
    ]


def test_normalise_openai_response_maps_tool_calls():
    raw = {
        "id": "chatcmpl-1",
        "model": "llama3.1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "Checking.",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "get_task_status",
                                "arguments": '{"task_id": "t-1"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }

    normalised = _normalise_openai_response(raw, "llama3.1")

    assert normalised["stop_reason"] == "tool_use"
    assert normalised["usage"]["input_tokens"] == 12
    assert normalised["usage"]["output_tokens"] == 3
    assert normalised["content"] == [
        {"type": "text", "text": "Checking."},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "get_task_status",
            "input": {"task_id": "t-1"},
        },
    ]


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
