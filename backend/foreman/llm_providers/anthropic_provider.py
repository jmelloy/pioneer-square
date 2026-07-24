"""Anthropic (Claude) provider adapter -- the direct api.anthropic.com API.

Client construction and chat-completion execution both delegate to the
existing, heavily-tested implementations in ``foreman.llm``
(``call_anthropic``) rather than reimplementing them, so behavior is
unchanged for every existing caller of that module. This adapter is the
formal, class-based entry point new code should use instead.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from .base import LLMProvider
from .errors import normalize_error

logger = logging.getLogger(__name__)

# Deployments sometimes set ANTHROPIC_API_KEY=placeholder as a non-empty
# stand-in (e.g. to satisfy tooling that requires the var to be present)
# without intending it as a real credential. Passing it to the SDK produces a
# confusing 401 instead of the "no key configured" fallback callers expect.
_PLACEHOLDER_KEY_VALUES = {"placeholder"}


def _real_api_key(value: str | None) -> str | None:
    """Return *value* unless it's blank or a known placeholder string."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() in _PLACEHOLDER_KEY_VALUES:
        return None
    return stripped


class AnthropicProvider(LLMProvider):
    """Adapter for the direct Anthropic Messages API."""

    name = "anthropic"

    def create_client(
        self,
        *,
        env: Mapping[str, str],
        model: str | None = None,
        anthropic_module: Any = None,
        api_key: str | None = None,
        **_ignored: Any,
    ) -> Any:
        """Build an ``AsyncAnthropic`` client.

        ``anthropic_module`` is the imported ``anthropic`` package. It is
        accepted as a parameter (rather than imported here) so
        ``foreman.llm.make_anthropic_client`` can forward its own
        module-level, test-patchable ``_anthropic_mod`` through unchanged; if
        omitted, ``anthropic`` is imported directly.

        ``api_key``/auth_token/base_url are resolved from ``env``, with an
        explicit ``api_key`` argument taking priority -- except over a
        guild-configured ``ANTHROPIC_AUTH_TOKEN``, which always wins so the
        SDK never receives both (it raises ``ValueError`` if it does).
        """
        if anthropic_module is None:
            import anthropic as anthropic_module  # noqa: PLC0415

        kwargs: dict[str, Any] = {}
        if env.get("ANTHROPIC_AUTH_TOKEN"):
            kwargs["auth_token"] = env["ANTHROPIC_AUTH_TOKEN"]
        else:
            resolved_api_key = _real_api_key(api_key) or _real_api_key(env.get("ANTHROPIC_API_KEY"))
            if resolved_api_key:
                kwargs["api_key"] = resolved_api_key
        if env.get("ANTHROPIC_BASE_URL"):
            kwargs["base_url"] = env["ANTHROPIC_BASE_URL"]
        logger.info(
            "Foreman using Anthropic API (auth=%s, base_url=%s)",
            "auth-token"
            if kwargs.get("auth_token")
            else ("api-key" if kwargs.get("api_key") else "default"),
            kwargs.get("base_url", "default"),
        )
        return anthropic_module.AsyncAnthropic(**kwargs)

    async def chat_completion(
        self,
        client: Any,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> tuple[Any, str | None]:
        try:
            from foreman.llm import call_anthropic
        except ImportError:  # pragma: no cover - exercised under the proxy's import layout
            from backend.foreman.llm import call_anthropic

        try:
            return await call_anthropic(
                client,
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
            )
        except Exception as exc:
            raise normalize_error(exc, provider=self.name) from exc

    def stream_chat_completion(
        self,
        client: Any,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
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
        )

    async def _stream(
        self,
        client: Any,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
    ) -> AsyncIterator[str]:
        try:
            from foreman.llm import _has_payload
        except ImportError:  # pragma: no cover - exercised under the proxy's import layout
            from backend.foreman.llm import _has_payload

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system or [],
            "messages": messages or [],
        }
        if _has_payload(tools):
            kwargs["tools"] = tools
        if _has_payload(tool_choice):
            kwargs["tool_choice"] = tool_choice

        try:
            async with client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as exc:
            raise normalize_error(exc, provider=self.name) from exc
