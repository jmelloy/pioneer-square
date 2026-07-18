"""OpenAI-compatible provider adapter (Ollama, vLLM, LM Studio, ...).

POSTs to ``/chat/completions``; requests/responses are translated to and from
the Anthropic Messages API shape via the existing helpers in ``foreman.llm``
(``call_openai_compatible`` and friends). Streaming is new: the non-streaming
path never needed it, so this adapter implements SSE parsing directly against
the OpenAI-compatible wire format.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from .base import LLMProvider
from .errors import normalize_error

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """Adapter for any OpenAI-compatible ``/chat/completions`` endpoint."""

    name = "openai"

    def create_client(
        self,
        *,
        env: Mapping[str, str],
        model: str | None = None,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> httpx.AsyncClient:
        """Build the httpx client used for every request to the endpoint.

        Unlike the Anthropic/Bedrock adapters, base_url/api_key are not baked
        into the client -- they're passed per-call to ``chat_completion`` /
        ``stream_chat_completion`` -- so one client can be reused across
        guilds with different endpoints if a caller chooses to.
        """
        return httpx.AsyncClient(timeout=timeout)

    async def chat_completion(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        base_url: str,
        api_key: str | None = None,
        **_ignored: Any,
    ) -> tuple[dict[str, Any], str | None]:
        try:
            from foreman.llm import call_openai_compatible
        except ImportError:  # pragma: no cover - exercised under the proxy's import layout
            from backend.foreman.llm import call_openai_compatible

        try:
            return await call_openai_compatible(
                client,
                model=model,
                max_tokens=max_tokens,
                base_url=base_url,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                api_key=api_key,
            )
        except Exception as exc:
            raise normalize_error(exc, provider=self.name) from exc

    def stream_chat_completion(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        base_url: str,
        api_key: str | None = None,
        **_ignored: Any,
    ) -> AsyncIterator[str]:
        return self._stream(
            client,
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            base_url=base_url,
            api_key=api_key,
        )

    async def _stream(
        self,
        client: httpx.AsyncClient,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
        base_url: str,
        api_key: str | None,
    ) -> AsyncIterator[str]:
        try:
            from foreman.llm import (
                _has_payload,
                anthropic_messages_to_openai,
                anthropic_tools_to_openai,
            )
        except ImportError:  # pragma: no cover - exercised under the proxy's import layout
            from backend.foreman.llm import (
                _has_payload,
                anthropic_messages_to_openai,
                anthropic_tools_to_openai,
            )

        body: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages_to_openai(system or [], messages or []),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if _has_payload(tools):
            body["tools"] = anthropic_tools_to_openai(tools)
        if _has_payload(tool_choice):
            body["tool_choice"] = "none" if tool_choice.get("type") == "none" else tool_choice

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = f"{base_url.rstrip('/')}/chat/completions"
        try:
            async with client.stream("POST", url, json=body, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning(
                            "OpenAI-compatible stream sent invalid JSON chunk: %.200r", payload
                        )
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
        except Exception as exc:
            raise normalize_error(exc, provider=self.name) from exc
