"""Shared LLM provider logic: client factory, the Anthropic call, and OpenAI-
compatible request/response translation.

Both the embedded foreman (backend/foreman/runner.py) and the standalone proxy
(foreman/pioneer_foreman/runner.py) import this module for ALL provider
normalization — this is the one place that knows the shape of each provider's
wire format. Neither runner re-implements or duplicates any of it.

Architecture boundary (see issue #826): the proxy exists only to decide *which
machine* makes the outbound call — a different Bedrock region/profile, a
locally-reachable OpenAI-compatible endpoint (e.g. Ollama), whatever the
operator configured. It holds no provider-specific logic of its own; it just
plumbs its own config into the helpers below. The embedded foreman calls
`make_anthropic_client` + `call_anthropic` directly for the providers the
Anthropic SDK can reach itself (`ANTHROPIC_SDK_PROVIDERS`); for anything else
(currently just `"openai"`) it requires a connected foreman API proxy rather
than calling `call_openai_compatible` locally — see the FOREMAN_PROVIDER check
in `backend/foreman/runner.py`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx

try:
    # Absolute import works when backend/ itself is on sys.path (embedded
    # backend, worker). The standalone foreman proxy only prepends the repo
    # root (see foreman-proxy/pioneer_foreman/config.py), under which this
    # module is reachable as backend.foreman.llm and util/ has no top-level
    # name of its own — fall back to the fully-qualified path there.
    from util.api_latency import track_api_call
except ImportError:  # pragma: no cover - exercised under the proxy's import layout
    from backend.util.api_latency import track_api_call

try:
    import anthropic as _anthropic_mod

    HAS_ANTHROPIC = True
except ImportError:
    _anthropic_mod = None  # type: ignore[assignment]
    HAS_ANTHROPIC = False

logger = logging.getLogger(__name__)

# Providers the Anthropic SDK (make_anthropic_client) can call directly, either
# against the direct API or Amazon Bedrock. Any other provider value (e.g.
# "openai") needs call_openai_compatible, which the embedded foreman never
# calls locally — only the standalone proxy does, since that's the process an
# operator points at their own OpenAI-compatible endpoint.
ANTHROPIC_SDK_PROVIDERS = frozenset({"anthropic", "bedrock"})


class BedrockModelNotConfiguredError(ValueError):
    """Raised when provider=bedrock is selected but no model/inference-profile is configured.

    Bedrock has no universal default model — inference profile ARNs are scoped to a
    single AWS account — so there is no safe value to fall back to silently. Callers
    must configure one explicitly (guild settings `model` field or FOREMAN_BEDROCK_MODEL).
    """


FOREMAN_MODEL = os.environ.get("FOREMAN_MODEL", "claude-sonnet-4-6")
# Bedrock uses cross-region inference profiles, not plain model IDs, and those
# profiles are scoped to a single AWS account, so there is no valid cross-account
# default. Set this to the profile ARN/ID appropriate for your account/region, e.g.:
#   arn:aws:bedrock:us-east-1:<account-id>:inference-profile/us.anthropic.claude-sonnet-4-6
FOREMAN_BEDROCK_MODEL = os.environ.get("FOREMAN_BEDROCK_MODEL")

# Set FOREMAN_PROVIDER=bedrock to use Amazon Bedrock instead of the Anthropic API.
# Requires: pip install "anthropic[bedrock]"  +  AWS credentials in env / IAM role.
FOREMAN_PROVIDER = os.environ.get("FOREMAN_PROVIDER", "anthropic").lower()
_BEDROCK_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def get_foreman_model(
    provider: str | None = None,
    *,
    model: str | None = None,
    bedrock_model: str | None = None,
) -> str:
    """Return the model ID to use for the given provider.

    When provider is 'bedrock' (or FOREMAN_PROVIDER=bedrock), returns
    bedrock_model (falling back to FOREMAN_BEDROCK_MODEL), raising
    BedrockModelNotConfiguredError if both are unset — Bedrock inference-profile
    ARNs are AWS-account-scoped, so there is no safe default to fall back to.
    Otherwise returns model (falling back to FOREMAN_MODEL).

    model/bedrock_model let callers with their own config source (e.g. the
    standalone foreman's Config dataclass) pass explicit overrides instead of
    duplicating this provider-branching logic against their own fields.

    Reads os.environ on every call (when no explicit override is given) so
    that tests can patch env vars directly.
    """
    resolved = (provider or os.environ.get("FOREMAN_PROVIDER", "anthropic")).lower()
    if resolved == "bedrock":
        resolved_bedrock_model = (
            bedrock_model if bedrock_model is not None else os.environ.get("FOREMAN_BEDROCK_MODEL")
        )
        if not resolved_bedrock_model:
            raise BedrockModelNotConfiguredError(
                "Bedrock provider selected but no model is configured. Unlike AWS "
                "credentials, which boto3 can resolve on its own (IAM role, profile, "
                "env vars) and report clearly if missing, there is no discoverable "
                "default model or inference profile — the Bedrock API call itself "
                "requires one to be passed explicitly, and an account-scoped ARN "
                "guessed here could silently send requests to the wrong AWS account. "
                "Set a model (inference-profile ARN or model ID) in the guild's "
                "foreman settings, or set the FOREMAN_BEDROCK_MODEL environment "
                "variable."
            )
        return resolved_bedrock_model
    return model if model is not None else os.environ.get("FOREMAN_MODEL", "claude-sonnet-4-6")


def _log_bedrock_credentials(
    region: str | None,
    profile: str | None,
    *,
    access_key: str | None = None,
    secret_key: str | None = None,
    session_token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Probe boto3's credential chain and log what it resolves (or why it can't).

    The anthropic SDK builds a ``boto3.Session`` and raises the opaque
    ``"could not resolve credentials from session"`` when
    ``session.get_credentials()`` returns ``None``. We replicate that session
    here at client-creation time so the *reason* (no creds, wrong profile,
    expired SSO token, missing region) shows up in the logs instead.

    ``env`` is the effective credential source (guild env_vars overlaid on
    os.environ); explicit access/secret keys, when given, mirror what we pass
    to the SDK so the probe reflects the real resolution.
    """
    env = env if env is not None else os.environ
    # Surface the relevant env vars regardless of whether boto3 imports.
    env_summary = {
        "AWS_PROFILE": env.get("AWS_PROFILE"),
        "AWS_BEARER_TOKEN_BEDROCK": "set" if env.get("AWS_BEARER_TOKEN_BEDROCK") else None,
        "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION"),
        "AWS_REGION": env.get("AWS_REGION"),
        "AWS_ACCESS_KEY_ID": "set" if (access_key or env.get("AWS_ACCESS_KEY_ID")) else None,
        "AWS_SECRET_ACCESS_KEY": "set"
        if (secret_key or env.get("AWS_SECRET_ACCESS_KEY"))
        else None,
        "AWS_SESSION_TOKEN": "set" if (session_token or env.get("AWS_SESSION_TOKEN")) else None,
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"),
        "AWS_WEB_IDENTITY_TOKEN_FILE": env.get("AWS_WEB_IDENTITY_TOKEN_FILE"),
    }
    logger.info(
        "Bedrock credential probe: requested region=%s profile=%s explicit_keys=%s; env=%s",
        region,
        profile,
        bool(access_key and secret_key),
        {k: v for k, v in env_summary.items() if v is not None},
    )

    try:
        import boto3  # noqa: PLC0415  (lazy: only needed for the bedrock path)
    except ImportError:
        logger.warning(
            "Bedrock credential probe: boto3 not importable; install "
            "anthropic[bedrock]. Falling back to SDK credential resolution."
        )
        return

    try:
        session = boto3.Session(
            profile_name=profile,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
    except Exception as exc:  # e.g. ProfileNotFound
        logger.error(
            "Bedrock credential probe: failed to build boto3 session "
            "(profile=%s region=%s): %s: %s",
            profile,
            region,
            type(exc).__name__,
            exc,
        )
        return

    if not session.region_name:
        logger.warning(
            "Bedrock credential probe: no region resolved (pass region or set "
            "AWS_DEFAULT_REGION); SigV4 signing will fail without one."
        )

    try:
        creds = session.get_credentials()
    except Exception as exc:  # e.g. SSO token expired, unable to load credentials
        logger.error(
            "Bedrock credential probe: get_credentials() raised %s: %s "
            "(this is the cause of 'could not resolve credentials from session')",
            type(exc).__name__,
            exc,
        )
        return

    if creds is None:
        logger.error(
            "Bedrock credential probe: boto3 resolved NO credentials. This is "
            "exactly what makes the anthropic SDK raise 'could not resolve "
            "credentials from session'. Checked profile=%s. Provide credentials "
            "via AWS_PROFILE, ~/.aws/credentials, env keys, or an IAM role. "
            "(When configuring via the guild settings dialogue, AWS_* env vars "
            "must reach the foreman process, not just spawned workers.)",
            profile or env.get("AWS_PROFILE") or "default",
        )
        return

    method = getattr(creds, "method", "unknown")
    frozen = creds.get_frozen_credentials()
    logger.info(
        "Bedrock credential probe: resolved credentials via method=%s "
        "(access_key=…%s, session_token=%s, region=%s)",
        method,
        (frozen.access_key or "")[-4:],
        "present" if frozen.token else "none",
        session.region_name,
    )


def make_anthropic_client(
    provider: str | None = None,
    api_key: str | None = None,
    region: str | None = None,
    aws_profile: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    model: str | None = None,
):
    """Create and return an Anthropic async client.

    provider:    'anthropic' (default, reads FOREMAN_PROVIDER env) or 'bedrock'.
    api_key:     API key for direct Anthropic API (ignored for Bedrock).
    region:      AWS region for Bedrock (defaults to AWS_DEFAULT_REGION or 'us-east-1').
    aws_profile: Named AWS profile for Bedrock (defaults to AWS_PROFILE env).
    extra_env:   Guild-configured env vars (settings dialogue) overlaid on
                 os.environ for credential resolution — AWS_* for Bedrock,
                 ANTHROPIC_* for the direct API. These do NOT reach the foreman
                 process otherwise: the dialogue's env_vars are only injected
                 into spawned workers, never this process.
    model:       Resolved model/inference-profile for Bedrock (e.g. the guild's
                 configured model), checked against FOREMAN_BEDROCK_MODEL when
                 absent. Ignored for the direct Anthropic API. Callers that
                 build a client before resolving the model (see get_foreman_model)
                 would otherwise only discover a missing Bedrock model deep
                 inside the first API call; passing it here fails fast instead.
    """
    if not HAS_ANTHROPIC or _anthropic_mod is None:
        raise ImportError("anthropic package is not installed")

    # Read env dynamically (same as get_foreman_model) so patching os.environ in
    # tests works and a late-set FOREMAN_PROVIDER env var is picked up correctly.
    resolved_provider = (provider or os.environ.get("FOREMAN_PROVIDER", "anthropic")).lower()
    # Effective credential source: guild-supplied env vars take precedence over
    # the foreman process env (guild settings are the more specific config).
    # Explicit args still win over both.
    env: Mapping[str, str] = {**os.environ, **(extra_env or {})}

    if resolved_provider == "bedrock":
        # Fail fast: a Bedrock client with no model would only surface this
        # deep inside the first API call. Check it here, before the client
        # (and any of its credential resolution) is even built — consistent
        # with the same check in get_foreman_model().
        resolved_model = model or env.get("FOREMAN_BEDROCK_MODEL")
        if not resolved_model:
            raise BedrockModelNotConfiguredError(
                "Bedrock provider selected but no model is configured. Unlike AWS "
                "credentials, which boto3 can resolve on its own (IAM role, profile, "
                "env vars) and report clearly if missing, there is no discoverable "
                "default model or inference profile — the Bedrock API call itself "
                "requires one to be passed explicitly, and an account-scoped ARN "
                "guessed here could silently send requests to the wrong AWS account. "
                "Set a model (inference-profile ARN or model ID) in the guild's "
                "foreman settings, or set the FOREMAN_BEDROCK_MODEL environment "
                "variable."
            )
        resolved_region = (
            region or env.get("AWS_DEFAULT_REGION") or env.get("AWS_REGION") or _BEDROCK_REGION
        )
        resolved_profile = aws_profile or env.get("AWS_PROFILE") or None

        # Bedrock also hosts foundation models with their own wire format —
        # Amazon Nova, Kimi K2, ... — that the Anthropic SDK's Messages API
        # cannot reach. Those go through the Bedrock Runtime Converse API
        # instead (see providers/bedrock.py); everything else on this path
        # is Claude-on-Bedrock via AsyncAnthropicBedrock, unchanged.
        try:
            # See the util.api_latency import fallback above for why both
            # layouts are needed here.
            from foreman.providers.bedrock import BedrockNativeClient, is_native_bedrock_model
        except ImportError:  # pragma: no cover - exercised under the proxy's import layout
            from backend.foreman.providers.bedrock import (
                BedrockNativeClient,
                is_native_bedrock_model,
            )

        if is_native_bedrock_model(resolved_model):
            logger.info(
                "Foreman using Amazon Bedrock Converse API (region=%s, profile=%s, auth=%s, model=%s)",
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

        access_key = env.get("AWS_ACCESS_KEY_ID")
        secret_key = env.get("AWS_SECRET_ACCESS_KEY")
        session_token = env.get("AWS_SESSION_TOKEN")
        # Bearer-token auth (AWS_BEARER_TOKEN_BEDROCK) bypasses SigV4/boto3
        # entirely. The SDK reads it into `api_key` and *raises* if you also
        # pass any AWS credential (aws_profile/keys), so when a token is present
        # we must not pass any AWS credential and we skip the boto3 probe.
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
        bedrock_kwargs: dict = {"aws_region": resolved_region}
        if bearer_token:
            logger.info(
                "Bedrock credential probe: AWS_BEARER_TOKEN_BEDROCK is set "
                "(len=%d); using bearer-token auth, skipping boto3 SigV4 "
                "resolution.",
                len(bearer_token),
            )
            bedrock_kwargs["api_key"] = bearer_token
        else:
            # Probe up front so a missing/expired credential shows up in the
            # logs with a clear reason rather than the SDK's opaque
            # RuntimeError later.
            _log_bedrock_credentials(
                resolved_region,
                resolved_profile,
                access_key=access_key,
                secret_key=secret_key,
                session_token=session_token,
                env=env,
            )
            # Pass credentials explicitly so resolution is deterministic and
            # matches what the probe validated. Only safe when there's no
            # bearer token. Explicit keys (from guild env_vars) take priority
            # over a profile, mirroring boto3's own precedence.
            if access_key and secret_key:
                bedrock_kwargs["aws_access_key"] = access_key
                bedrock_kwargs["aws_secret_key"] = secret_key
                if session_token:
                    bedrock_kwargs["aws_session_token"] = session_token
            elif resolved_profile:
                bedrock_kwargs["aws_profile"] = resolved_profile
        return _anthropic_mod.AsyncAnthropicBedrock(**bedrock_kwargs)

    # Direct Anthropic API. The SDK reads ANTHROPIC_* from os.environ on its own,
    # but guild-configured env_vars never reach this process, so resolve them
    # from the merged `env` and pass explicitly.
    # api_key and auth_token are mutually exclusive; auth_token takes precedence
    # so a guild-configured ANTHROPIC_AUTH_TOKEN overrides a process-level
    # ANTHROPIC_API_KEY rather than causing an SDK ValueError.
    kwargs: dict = {}
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        kwargs["auth_token"] = env["ANTHROPIC_AUTH_TOKEN"]
    else:
        resolved_api_key = api_key or env.get("ANTHROPIC_API_KEY")
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
    return _anthropic_mod.AsyncAnthropic(**kwargs)


# ---------------------------------------------------------------------------
# Anthropic Messages API call
# ---------------------------------------------------------------------------


def _has_payload(value: dict[str, Any] | list[Any] | None) -> bool:
    """True when `value` is a non-None, non-empty dict/list.

    Explicit rather than a bare truthiness check so intent is clear and a
    future falsy-but-meaningful value (e.g. `False`) wouldn't be silently
    dropped the way an empty `tools`/`tool_choice` should be.
    """
    return value is not None and value != {} and value != []


async def call_anthropic(
    client: Any,
    *,
    model: str,
    max_tokens: int,
    system: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
) -> tuple[Any, str | None]:
    """Execute one Anthropic Messages API call. Returns (parsed_response, request_id).

    `client` is any Anthropic SDK async client from make_anthropic_client
    (AsyncAnthropic or AsyncAnthropicBedrock — Bedrock speaks the same Messages
    API shape, so one call path covers both). Both the embedded foreman and the
    standalone proxy call this against whichever client their own config
    selected, so the request/response shape only has to be gotten right here.

    `parsed_response` is the SDK's own response model (exposing both attribute
    access like `.content`/`.usage` and `.model_dump()`) — callers that need a
    plain dict (e.g. the proxy, to send the response back over the wire) call
    `.model_dump()` themselves; callers that stay in-process (the embedded
    foreman's local call path) can use it directly.

    `tools` and `tool_choice` are only included in the request when non-empty:
    some Anthropic API endpoints (e.g. Bedrock) reject an explicit empty
    `tools` array or `tool_choice` object, and omitting the param (rather than
    defaulting a missing/None value to `[]`/`{}`) also means a caller that
    explicitly passes `tools=None`/`tool_choice=None` isn't silently overridden.
    """
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

    provider = "bedrock" if "Bedrock" in type(client).__name__ else "anthropic"
    with track_api_call(provider, model, method="messages.create") as call:
        raw = await client.messages.with_raw_response.create(**kwargs)
        parsed = raw.parse()
        call.status_code = 200
        usage = getattr(parsed, "usage", None)
        if usage is not None:
            call.input_tokens = getattr(usage, "input_tokens", None)
            call.output_tokens = getattr(usage, "output_tokens", None)
    return parsed, raw.headers.get("request-id")


# ---------------------------------------------------------------------------
# OpenAI-compatible provider: translation to/from the Anthropic Messages API
# shape that the rest of this codebase (history storage, tool-use parsing,
# call_anthropic above) speaks natively.
# ---------------------------------------------------------------------------


def _anthropic_content_to_text(blocks: Any) -> str:
    """Flatten Anthropic content (a string, or a list of text/tool_result blocks)
    into plain text — OpenAI chat messages take a single string, not blocks."""
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


def anthropic_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool schemas (name/description/input_schema) to OpenAI's
    function-calling schema."""
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


def _parse_openai_tool_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("OpenAI-compatible tool call returned invalid JSON arguments: %.200r", raw)
        return {"_raw_arguments": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def anthropic_messages_to_openai(
    system: list[dict[str, Any]], messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert an Anthropic system+messages pair into an OpenAI chat `messages` list.

    Anthropic `tool_use`/`tool_result` blocks become OpenAI `tool_calls` and
    `role: "tool"` messages respectively; everything else is flattened to text.
    """
    converted: list[dict[str, Any]] = []
    system_text = _anthropic_content_to_text(system)
    if system_text:
        converted.append({"role": "system", "content": system_text})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and isinstance(content, list):
            text = _anthropic_content_to_text(
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
                    text = _anthropic_content_to_text([block])
                    if text:
                        pending_text.append(text)
            if pending_text:
                converted.append({"role": "user", "content": "\n".join(pending_text)})
            continue

        converted.append({"role": role, "content": _anthropic_content_to_text(content)})

    return converted


def openai_response_to_anthropic(raw: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert an OpenAI chat-completion response into an Anthropic Messages API
    response dict, so callers only ever handle one response shape."""
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
                "input": _parse_openai_tool_arguments(fn.get("arguments")),
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


async def call_openai_compatible(
    client: httpx.AsyncClient,
    *,
    model: str,
    max_tokens: int,
    base_url: str,
    system: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Execute one OpenAI-compatible POST /chat/completions call.

    Takes the same Anthropic-shaped, snake_case keyword arguments as
    call_anthropic (model/max_tokens/system/messages/tools/tool_choice) rather
    than a raw request dict, so the two provider call paths share one calling
    convention. The caller (the standalone proxy) is responsible for
    translating its own wire format (the backend's camelCase
    maxTokens/toolChoice JSON) into these kwargs — that's the "which machine"
    plumbing the proxy owns; everything below is the provider translation this
    module owns. The request is converted to OpenAI chat-completion format,
    POSTed, and the response is translated back to an Anthropic Messages API
    response dict, so callers don't need to know which wire format actually
    served the request. Returns (response_dict, request_id).
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages_to_openai(system or [], messages or []),
        "max_tokens": max_tokens,
    }
    if _has_payload(tools):
        body["tools"] = anthropic_tools_to_openai(tools)
    if _has_payload(tool_choice):
        body["tool_choice"] = "none" if tool_choice.get("type") == "none" else tool_choice

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = f"{base_url.rstrip('/')}/chat/completions"
    with track_api_call("openai", url, method="POST") as call:
        response = await client.post(url, json=body, headers=headers)
        call.status_code = response.status_code
        response.raise_for_status()
        response_json = response.json()
        usage = response_json.get("usage") or {}
        call.input_tokens = usage.get("prompt_tokens")
        call.output_tokens = usage.get("completion_tokens")
    request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
    return openai_response_to_anthropic(response_json, model), request_id
