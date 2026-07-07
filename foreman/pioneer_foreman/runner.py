"""LLM API execution for the standalone Foreman proxy.

The backend sends complete Anthropic-shaped Messages API requests over the
WebSocket. This module executes that one request against the operator-selected
provider and returns an Anthropic-shaped response. It does not read or mutate
Pioneer Square state.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from backend.foreman.llm import HAS_ANTHROPIC, make_anthropic_client

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

_anthropic_clients: dict[tuple, object] = {}
_http_clients: dict[tuple, httpx.AsyncClient] = {}


def _get_anthropic_client(config: Config):
    provider = getattr(config, "provider", "anthropic") or "anthropic"
    region = getattr(config, "aws_region", None)
    profile = getattr(config, "aws_profile", None)
    api_key = getattr(config, "api_key", None) or ""
    anthropic_auth_token = getattr(config, "anthropic_auth_token", None) or ""
    cache_key = (provider.lower(), region, profile, api_key, anthropic_auth_token)
    if cache_key not in _anthropic_clients:
        extra_env: dict[str, str] = {}
        if anthropic_auth_token:
            extra_env["ANTHROPIC_AUTH_TOKEN"] = anthropic_auth_token
        _anthropic_clients[cache_key] = make_anthropic_client(
            provider=provider,
            api_key=api_key or None,
            region=region,
            aws_profile=profile,
            extra_env=extra_env or None,
            model=getattr(config, "bedrock_model", None),
        )
    return _anthropic_clients[cache_key]


def _get_http_client(config: Config) -> httpx.AsyncClient:
    key = (config.openai_base_url, config.api_key or "")
    if key not in _http_clients:
        _http_clients[key] = httpx.AsyncClient(timeout=config.api_timeout)
    return _http_clients[key]


def _text_from_blocks(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return "" if blocks is None else str(blocks)
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                parts.append(str(block.get("content") or ""))
    return "\n".join(p for p in parts if p)


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {},
            },
        }
        for tool in tools
    ]


def _parse_tool_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("OpenAI-compatible tool call returned invalid JSON arguments: %.200r", raw)
        return {"_raw_arguments": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _openai_messages(
    system: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    system_text = _text_from_blocks(system)
    if system_text:
        converted.append({"role": "system", "content": system_text})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and isinstance(content, list):
            text = _text_from_blocks(
                [
                    block
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
            )
            tool_calls = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_calls.append(
                    {
                        "id": block.get("id") or f"toolu_{uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    }
                )
            converted.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                }
            )
            continue

        if role == "user" and isinstance(content, list):
            pending_text: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if pending_text:
                        converted.append({"role": "user", "content": "\n".join(pending_text)})
                        pending_text = []
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id") or "",
                            "content": str(block.get("content") or ""),
                        }
                    )
                else:
                    text = _text_from_blocks([block])
                    if text:
                        pending_text.append(text)
            if pending_text:
                converted.append({"role": "user", "content": "\n".join(pending_text)})
            continue

        converted.append({"role": role, "content": _text_from_blocks(content)})

    return converted


def _normalise_openai_response(raw: dict[str, Any], model: str) -> dict[str, Any]:
    choice = (raw.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content_blocks: list[dict[str, Any]] = []
    if message.get("content"):
        content_blocks.append({"type": "text", "text": str(message["content"])})

    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid4().hex}",
                "name": fn.get("name") or "",
                "input": _parse_tool_arguments(fn.get("arguments")),
            }
        )

    finish_reason = choice.get("finish_reason")
    stop_reason = {
        "tool_calls": "tool_use",
        "stop": "end_turn",
        "length": "max_tokens",
    }.get(finish_reason, finish_reason or "end_turn")
    usage = raw.get("usage") or {}
    return {
        "id": raw.get("id") or f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": raw.get("model") or model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or 0,
            "output_tokens": usage.get("completion_tokens", 0) or 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


async def _call_openai_compatible(request: dict[str, Any], config: Config) -> dict[str, Any]:
    model = config.effective_model or request["model"]
    body: dict[str, Any] = {
        "model": model,
        "messages": _openai_messages(request.get("system") or [], request.get("messages") or []),
        "tools": _openai_tools(request.get("tools") or []),
        "max_tokens": request.get("maxTokens") or 1024,
    }
    tool_choice = request.get("toolChoice")
    if tool_choice:
        body["tool_choice"] = "none" if tool_choice.get("type") == "none" else tool_choice

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    client = _get_http_client(config)
    url = f"{config.openai_base_url.rstrip('/')}/chat/completions"
    response = await client.post(url, json=body, headers=headers)
    response.raise_for_status()
    return {
        "response": _normalise_openai_response(response.json(), model),
        "apiRequestId": response.headers.get("x-request-id") or response.headers.get("request-id"),
        "provider": "openai",
        "model": model,
    }


async def _call_anthropic(request: dict[str, Any], config: Config) -> dict[str, Any]:
    if not HAS_ANTHROPIC:
        raise RuntimeError("anthropic package is not installed")
    model = config.effective_model or request["model"]
    client = _get_anthropic_client(config)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": request.get("maxTokens") or 1024,
        "system": request.get("system") or [],
        "messages": request.get("messages") or [],
        "tools": request.get("tools") or [],
    }
    if request.get("toolChoice") is not None:
        kwargs["tool_choice"] = request["toolChoice"]
    raw = await client.messages.with_raw_response.create(**kwargs)
    parsed = raw.parse()
    return {
        "response": parsed.model_dump(),
        "apiRequestId": raw.headers.get("request-id"),
        "provider": config.provider,
        "model": model,
    }


async def run_api_request(request: dict[str, Any], config: Config) -> dict[str, Any]:
    """Execute one backend-supplied LLM API request and return a response envelope."""
    provider = (config.provider or "anthropic").lower()
    if provider == "openai":
        return await _call_openai_compatible(request, config)
    if provider in {"anthropic", "bedrock"}:
        return await _call_anthropic(request, config)
    raise ValueError(f"Unsupported foreman proxy provider: {provider}")


async def close_clients() -> None:
    """Close cached HTTP clients used by OpenAI-compatible providers."""
    for client in list(_http_clients.values()):
        await client.aclose()
    _http_clients.clear()
