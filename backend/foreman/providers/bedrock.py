"""Amazon Bedrock support for non-Anthropic foundation models (Amazon Nova, Kimi K2, ...).

Claude-on-Bedrock is served through the Anthropic SDK's ``AsyncAnthropicBedrock``
client (see ``backend/foreman/llm.py``) because it already speaks the Anthropic
Messages API shape end to end. Bedrock also hosts foundation models with their
own wire format — Amazon Nova, Kimi K2, and others — that the Anthropic SDK
cannot call; those only speak the Bedrock Runtime Converse API. This module is
the one place that calls it, translating requests/responses to and from the
Anthropic Messages shape so the rest of the codebase (history storage,
tool-use parsing, ``foreman.llm.call_anthropic``) never has to know which wire
format actually served the request — the same boundary ``call_openai_compatible``
draws for OpenAI-compatible endpoints.

``BedrockNativeClient`` below exposes just enough of the Anthropic SDK client
surface (``.messages.with_raw_response.create(**kwargs)``) for
``foreman.llm.call_anthropic`` to use it unmodified: ``make_anthropic_client()``
returns one of these instead of ``AsyncAnthropicBedrock`` when the configured
model is a native (non-Anthropic) Bedrock model, and everything downstream
proceeds exactly as it does for Claude-on-Bedrock.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

try:
    # Absolute import works when backend/ itself is on sys.path (embedded
    # backend, worker); the standalone foreman proxy prepends the repo root
    # instead, under which this module has no top-level name of its own — fall
    # back to the fully-qualified path there. See the matching fallback in
    # backend/foreman/llm.py.
    from util.api_latency import track_api_call
except ImportError:  # pragma: no cover - exercised under the proxy's import layout
    from backend.util.api_latency import track_api_call

try:
    import boto3

    HAS_BOTO3 = True
except ImportError:
    boto3 = None  # type: ignore[assignment]
    HAS_BOTO3 = False


# Model-ID prefixes (after stripping a cross-region "us."/"eu."/"ap." prefix)
# that identify a Bedrock foundation model with its own wire format, requiring
# the Converse API rather than AsyncAnthropicBedrock's Messages-API path.
# Deliberately an allowlist, not "anything without 'anthropic' in the name" —
# free-form model IDs used elsewhere (tests, placeholder ARNs) must keep going
# through the existing AsyncAnthropicBedrock path unchanged.
_NATIVE_MODEL_PREFIXES = (
    "amazon.nova",
    "amazon.titan",
    "moonshot.kimi",
    "moonshotai.kimi",
)

_REGION_PREFIX_RE = re.compile(r"^(?:us|eu|ap)\.")


def is_native_bedrock_model(model: str) -> bool:
    """True when *model* is a non-Anthropic Bedrock model (Nova, Kimi K2, ...)
    that must be called via the Converse API rather than AsyncAnthropicBedrock."""
    stripped = _REGION_PREFIX_RE.sub("", model.lower())
    return stripped.startswith(_NATIVE_MODEL_PREFIXES)


# ---------------------------------------------------------------------------
# boto3 client cache
# ---------------------------------------------------------------------------

_clients: dict[tuple[str, str | None, str | None, str | None], Any] = {}


def _get_client(region: str, profile: str | None, extra_env: Mapping[str, str] | None):
    if not HAS_BOTO3 or boto3 is None:
        raise ImportError("boto3 is not installed; add it to requirements.txt for Bedrock support")

    env: Mapping[str, str] = extra_env if extra_env is not None else os.environ
    access_key = env.get("AWS_ACCESS_KEY_ID")
    secret_key = env.get("AWS_SECRET_ACCESS_KEY")
    session_token = env.get("AWS_SESSION_TOKEN")

    cache_key = (region, profile, access_key, secret_key)
    if cache_key not in _clients:
        session_kwargs: dict[str, Any] = {"region_name": region}
        if access_key and secret_key:
            session_kwargs["aws_access_key_id"] = access_key
            session_kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                session_kwargs["aws_session_token"] = session_token
        elif profile:
            session_kwargs["profile_name"] = profile
        session = boto3.Session(**session_kwargs)
        _clients[cache_key] = session.client("bedrock-runtime")
    return _clients[cache_key]


# ---------------------------------------------------------------------------
# Anthropic Messages <-> Bedrock Converse translation
# ---------------------------------------------------------------------------


def _anthropic_content_to_converse_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            blocks.append({"text": block.get("text") or ""})
        elif btype == "tool_use":
            blocks.append(
                {
                    "toolUse": {
                        "toolUseId": block.get("id") or "",
                        "name": block.get("name") or "",
                        "input": block.get("input") or {},
                    }
                }
            )
        elif btype == "tool_result":
            result_content = block.get("content")
            if isinstance(result_content, str):
                result_blocks = [{"text": result_content}]
            elif isinstance(result_content, list):
                result_blocks = [
                    {"text": rc.get("text") or ""} for rc in result_content if isinstance(rc, dict)
                ] or [{"text": ""}]
            else:
                result_blocks = [{"text": str(result_content or "")}]
            tool_result: dict[str, Any] = {
                "toolUseId": block.get("tool_use_id") or "",
                "content": result_blocks,
            }
            if block.get("is_error"):
                tool_result["status"] = "error"
            blocks.append({"toolResult": tool_result})
    return blocks


def anthropic_messages_to_converse(
    system: list[dict[str, Any]] | None, messages: list[dict[str, Any]] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert an Anthropic system+messages pair into Converse's (system, messages)."""
    system_blocks = [{"text": b.get("text", "")} for b in (system or []) if b.get("text")]
    converse_messages: list[dict[str, Any]] = []
    for msg in messages or []:
        blocks = _anthropic_content_to_converse_blocks(msg.get("content"))
        if blocks:
            converse_messages.append({"role": msg.get("role"), "content": blocks})
    return system_blocks, converse_messages


def anthropic_tools_to_converse(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert Anthropic tool schemas into Converse's toolConfig shape."""
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": {"json": tool.get("input_schema") or {}},
                }
            }
            for tool in tools
        ]
    }


def anthropic_tool_choice_to_converse(tool_choice: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert an Anthropic tool_choice into Converse's toolChoice shape.

    Converse has no equivalent of Anthropic's "none" (force no tool call); that
    case is left to the caller to handle by omitting tools entirely.
    """
    if not tool_choice:
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return {"auto": {}}
    if choice_type == "any":
        return {"any": {}}
    if choice_type == "tool":
        return {"tool": {"name": tool_choice.get("name") or ""}}
    return None


_STOP_REASON_MAP = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}


def converse_response_to_anthropic(response: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert a Bedrock Converse API response into an Anthropic Messages API
    response dict, so callers only ever handle one response shape."""
    output_message = (response.get("output") or {}).get("message") or {}
    content_blocks: list[dict[str, Any]] = []
    for block in output_message.get("content") or []:
        if "text" in block:
            content_blocks.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            tool_use = block["toolUse"]
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": tool_use.get("toolUseId") or "",
                    "name": tool_use.get("name") or "",
                    "input": tool_use.get("input") or {},
                }
            )

    bedrock_stop_reason = response.get("stopReason")
    stop_reason = _STOP_REASON_MAP.get(bedrock_stop_reason, bedrock_stop_reason or "end_turn")

    usage = response.get("usage") or {}
    request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
    return {
        "id": request_id or f"msg_bedrock_{id(response)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("inputTokens", 0) or 0,
            "output_tokens": usage.get("outputTokens", 0) or 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


# ---------------------------------------------------------------------------
# Adapter matching the slice of the Anthropic SDK client interface that
# foreman.llm.call_anthropic() uses.
# ---------------------------------------------------------------------------


class _RawConverseResponse:
    """Mimics the Anthropic SDK's ``.with_raw_response`` wrapper: ``.parse()``
    returns the parsed response, ``.headers`` exposes the request id."""

    def __init__(self, parsed: SimpleNamespace, request_id: str | None):
        self._parsed = parsed
        self.headers = {"request-id": request_id} if request_id else {}

    def parse(self) -> SimpleNamespace:
        return self._parsed


def _response_dict_to_namespace(raw: dict[str, Any]) -> SimpleNamespace:
    content = [SimpleNamespace(**block) for block in raw.get("content") or []]
    usage = SimpleNamespace(**(raw.get("usage") or {}))
    return SimpleNamespace(
        id=raw.get("id"),
        type=raw.get("type", "message"),
        role=raw.get("role", "assistant"),
        model=raw.get("model"),
        content=content,
        stop_reason=raw.get("stop_reason"),
        stop_sequence=raw.get("stop_sequence"),
        usage=usage,
        model_dump=lambda: raw,
    )


class BedrockNativeClient:
    """Adapter exposing ``.messages.with_raw_response.create(**kwargs)`` — the
    slice of the Anthropic SDK client interface ``call_anthropic()`` needs —
    backed by the Bedrock Runtime Converse API instead of the Anthropic
    Messages API. Returned by ``make_anthropic_client()`` in place of
    ``AsyncAnthropicBedrock`` when the configured model is a native
    (non-Anthropic) Bedrock model such as Amazon Nova or Kimi K2.
    """

    def __init__(
        self,
        region: str,
        profile: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ):
        self._region = region
        self._profile = profile
        self._extra_env = extra_env
        self.messages = SimpleNamespace(with_raw_response=self)

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> _RawConverseResponse:
        system_blocks, converse_messages = anthropic_messages_to_converse(system, messages)
        request: dict[str, Any] = {
            "modelId": model,
            "messages": converse_messages,
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system_blocks:
            request["system"] = system_blocks
        if tools:
            tool_config = anthropic_tools_to_converse(tools)
            converse_choice = anthropic_tool_choice_to_converse(tool_choice)
            if converse_choice:
                tool_config["toolChoice"] = converse_choice
            request["toolConfig"] = tool_config

        client = _get_client(self._region, self._profile, self._extra_env)

        with track_api_call("bedrock", model, method="Converse") as call:
            response = await asyncio.to_thread(client.converse, **request)
            usage = response.get("usage") or {}
            call.status_code = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode", 200)
            call.input_tokens = usage.get("inputTokens")
            call.output_tokens = usage.get("outputTokens")

        response_dict = converse_response_to_anthropic(response, model)
        parsed = _response_dict_to_namespace(response_dict)
        request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
        return _RawConverseResponse(parsed, request_id)
