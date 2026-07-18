"""Amazon Bedrock provider adapter.

Covers both Claude-on-Bedrock (Anthropic Messages API via
``AsyncAnthropicBedrock``) and native foundation models -- Amazon Nova, Kimi
K2, ... -- that only speak Bedrock's Converse API (see
``backend/foreman/providers/bedrock.py``). Client construction mirrors
``foreman.llm.make_anthropic_client``'s Bedrock branch exactly (same kwargs,
same credential precedence); chat completions reuse ``foreman.llm.call_anthropic``
since both client types expose the same ``.messages.with_raw_response.create``
surface.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from .base import LLMProvider
from .errors import ProviderConfigError, normalize_error

logger = logging.getLogger(__name__)


class BedrockProvider(LLMProvider):
    """Adapter for Amazon Bedrock (Claude-on-Bedrock and native foundation models)."""

    name = "bedrock"

    def create_client(
        self,
        *,
        env: Mapping[str, str],
        model: str | None = None,
        anthropic_module: Any = None,
        region: str | None = None,
        aws_profile: str | None = None,
        default_region: str = "us-east-1",
        credential_logger: Callable[..., None] | None = None,
        **_ignored: Any,
    ) -> Any:
        """Build a Bedrock client.

        Returns a ``BedrockNativeClient`` (Converse API) for non-Anthropic
        foundation models, otherwise an ``AsyncAnthropicBedrock`` (Messages
        API). ``anthropic_module`` is the imported ``anthropic`` package,
        accepted as a parameter for the same reason as in
        ``AnthropicProvider.create_client``. ``default_region`` and
        ``credential_logger`` let ``foreman.llm.make_anthropic_client`` forward
        its own patchable module-level ``_BEDROCK_REGION`` /
        ``_log_bedrock_credentials`` through unchanged, rather than this
        adapter re-reading ``os.environ`` or re-probing boto3 credentials
        itself.

        Raises ``ProviderConfigError`` if no model/inference-profile is
        configured -- Bedrock inference-profile ARNs are AWS-account-scoped,
        so there is no safe default to fall back to.
        """
        try:
            from foreman.providers.bedrock import BedrockNativeClient, is_native_bedrock_model
        except ImportError:  # pragma: no cover - exercised under the proxy's import layout
            from backend.foreman.providers.bedrock import (
                BedrockNativeClient,
                is_native_bedrock_model,
            )

        resolved_model = model or env.get("FOREMAN_BEDROCK_MODEL")
        if not resolved_model:
            raise ProviderConfigError(
                "Bedrock provider selected but no model is configured. Unlike AWS "
                "credentials, which boto3 can resolve on its own (IAM role, profile, "
                "env vars) and report clearly if missing, there is no discoverable "
                "default model or inference profile - the Bedrock API call itself "
                "requires one to be passed explicitly, and an account-scoped ARN "
                "guessed here could silently send requests to the wrong AWS account. "
                "Set a model (inference-profile ARN or model ID) in the guild's "
                "foreman settings, or set the FOREMAN_BEDROCK_MODEL environment "
                "variable.",
                provider=self.name,
            )
        resolved_region = (
            region or env.get("AWS_DEFAULT_REGION") or env.get("AWS_REGION") or default_region
        )
        resolved_profile = aws_profile or env.get("AWS_PROFILE") or None

        if is_native_bedrock_model(resolved_model):
            logger.info(
                "Foreman using Amazon Bedrock Converse API (region=%s, profile=%s, "
                "auth=%s, model=%s)",
                resolved_region,
                resolved_profile,
                "bearer-token"
                if env.get("AWS_BEARER_TOKEN_BEDROCK")
                else (
                    "explicit-keys"
                    if (env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"))
                    else "sigv4"
                ),
                resolved_model,
            )
            return BedrockNativeClient(
                region=resolved_region, profile=resolved_profile, extra_env=env
            )

        if anthropic_module is None:
            import anthropic as anthropic_module  # noqa: PLC0415

        access_key = env.get("AWS_ACCESS_KEY_ID")
        secret_key = env.get("AWS_SECRET_ACCESS_KEY")
        session_token = env.get("AWS_SESSION_TOKEN")
        # Bearer-token auth (AWS_BEARER_TOKEN_BEDROCK) bypasses SigV4/boto3
        # entirely. The SDK reads it into `api_key` and *raises* if you also
        # pass any AWS credential, so when a token is present we must not
        # pass any AWS credential and we skip the boto3 probe.
        bearer_token = env.get("AWS_BEARER_TOKEN_BEDROCK")
        logger.info(
            "Foreman using Amazon Bedrock (region=%s, profile=%s, auth=%s, model=%s)",
            resolved_region,
            resolved_profile,
            "bearer-token"
            if bearer_token
            else ("explicit-keys" if (access_key and secret_key) else "sigv4"),
            resolved_model,
        )
        bedrock_kwargs: dict[str, Any] = {"aws_region": resolved_region}
        if bearer_token:
            logger.info(
                "Bedrock credential probe: AWS_BEARER_TOKEN_BEDROCK is set "
                "(len=%d); using bearer-token auth, skipping boto3 SigV4 "
                "resolution.",
                len(bearer_token),
            )
            bedrock_kwargs["api_key"] = bearer_token
        else:
            if credential_logger is not None:
                credential_logger(
                    resolved_region,
                    resolved_profile,
                    access_key=access_key,
                    secret_key=secret_key,
                    session_token=session_token,
                    env=env,
                )
            # Explicit keys (from guild env_vars) take priority over a
            # profile, mirroring boto3's own precedence.
            if access_key and secret_key:
                bedrock_kwargs["aws_access_key"] = access_key
                bedrock_kwargs["aws_secret_key"] = secret_key
                if session_token:
                    bedrock_kwargs["aws_session_token"] = session_token
            elif resolved_profile:
                bedrock_kwargs["aws_profile"] = resolved_profile
        return anthropic_module.AsyncAnthropicBedrock(**bedrock_kwargs)

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
        # AsyncAnthropicBedrock exposes the same `.messages.stream()` surface
        # as the direct Anthropic SDK client; BedrockNativeClient (Converse,
        # for Nova/Kimi K2/...) is a minimal adapter that does not implement
        # it yet.
        if not (hasattr(client, "messages") and hasattr(client.messages, "stream")):
            raise NotImplementedError(
                "Streaming is not yet supported for native Bedrock foundation "
                "models (Amazon Nova, Kimi K2, ...); use chat_completion() instead."
            )
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
