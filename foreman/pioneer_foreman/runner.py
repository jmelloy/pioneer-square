"""LLM API execution for the standalone Foreman proxy.

The backend sends complete Anthropic-shaped Messages API requests over the
WebSocket. This module is a thin wrapper: it only decides *which machine* (and
which client/credentials) executes that request — a Bedrock region/profile, an
OpenAI-compatible endpoint the operator configured (e.g. Ollama), whatever this
process's config selects. All provider-specific request/response translation
lives in backend.foreman.llm, shared with the embedded foreman; this module
holds none of its own. It does not read or mutate Pioneer Square state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from backend.foreman.llm import (
    ANTHROPIC_SDK_PROVIDERS,
    HAS_ANTHROPIC,
    call_anthropic,
    call_openai_compatible,
    make_anthropic_client,
)

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


async def _call_anthropic(request: dict[str, Any], config: Config) -> dict[str, Any]:
    if not HAS_ANTHROPIC:
        raise RuntimeError("anthropic package is not installed")
    model = config.effective_model or request["model"]
    client = _get_anthropic_client(config)
    parsed, request_id = await call_anthropic(
        client,
        model=model,
        max_tokens=request.get("maxTokens") or 1024,
        system=request.get("system") or [],
        messages=request.get("messages") or [],
        tools=request.get("tools") or [],
        tool_choice=request.get("toolChoice"),
    )
    return {
        "response": parsed.model_dump(),
        "apiRequestId": request_id,
        "provider": config.provider,
        "model": model,
    }


async def _call_openai_compatible(request: dict[str, Any], config: Config) -> dict[str, Any]:
    model = config.effective_model or request["model"]
    client = _get_http_client(config)
    response, request_id = await call_openai_compatible(
        client,
        request,
        model=model,
        base_url=config.openai_base_url,
        api_key=config.api_key,
    )
    return {"response": response, "apiRequestId": request_id, "provider": "openai", "model": model}


async def run_api_request(request: dict[str, Any], config: Config) -> dict[str, Any]:
    """Execute one backend-supplied LLM API request and return a response envelope."""
    provider = (config.provider or "anthropic").lower()
    if provider in ANTHROPIC_SDK_PROVIDERS:
        return await _call_anthropic(request, config)
    if provider == "openai":
        return await _call_openai_compatible(request, config)
    raise ValueError(f"Unsupported foreman proxy provider: {provider!r}")


async def close_clients() -> None:
    """Close cached HTTP clients used by OpenAI-compatible providers."""
    for client in list(_http_clients.values()):
        await client.aclose()
    _http_clients.clear()
