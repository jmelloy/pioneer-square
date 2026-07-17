"""Tests for the Bedrock Converse API provider wrapper (Amazon Nova, Kimi K2)."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman.providers.bedrock import (
    BedrockNativeClient,
    anthropic_messages_to_converse,
    anthropic_tool_choice_to_converse,
    anthropic_tools_to_converse,
    converse_response_to_anthropic,
    is_native_bedrock_model,
)

# ---------------------------------------------------------------------------
# is_native_bedrock_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "amazon.nova-pro-v1:0",
        "amazon.nova-lite-v1:0",
        "us.amazon.nova-pro-v1:0",
        "eu.amazon.titan-text-express-v1",
        "moonshot.kimi-k2-instruct",
        "moonshotai.kimi-k2-instruct",
        "ap.moonshotai.kimi-k2-instruct",
    ],
)
def test_is_native_bedrock_model_true(model):
    assert is_native_bedrock_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "anthropic.claude-sonnet-4-6-20251001-v1:0",
        "us.anthropic.claude-sonnet-4-6-20251001-v1:0",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-6",
        "claude-sonnet-4-6",
    ],
)
def test_is_native_bedrock_model_false(model):
    assert is_native_bedrock_model(model) is False


# ---------------------------------------------------------------------------
# Anthropic <-> Converse translation
# ---------------------------------------------------------------------------


def test_anthropic_messages_to_converse_text_only():
    system, messages = anthropic_messages_to_converse(
        [{"type": "text", "text": "be helpful"}],
        [{"role": "user", "content": "hello"}],
    )
    assert system == [{"text": "be helpful"}]
    assert messages == [{"role": "user", "content": [{"text": "hello"}]}]


def test_anthropic_messages_to_converse_tool_use_and_result():
    _, messages = anthropic_messages_to_converse(
        None,
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}}
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "result"}],
            },
        ],
    )
    assert messages[0]["content"] == [
        {"toolUse": {"toolUseId": "t1", "name": "search", "input": {"q": "x"}}}
    ]
    assert messages[1]["content"] == [
        {"toolResult": {"toolUseId": "t1", "content": [{"text": "result"}]}}
    ]


def test_anthropic_tools_to_converse():
    tool_config = anthropic_tools_to_converse(
        [{"name": "search", "description": "search the web", "input_schema": {"type": "object"}}]
    )
    assert tool_config == {
        "tools": [
            {
                "toolSpec": {
                    "name": "search",
                    "description": "search the web",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
    }


@pytest.mark.parametrize(
    "tool_choice,expected",
    [
        ({"type": "auto"}, {"auto": {}}),
        ({"type": "any"}, {"any": {}}),
        ({"type": "tool", "name": "search"}, {"tool": {"name": "search"}}),
        (None, None),
        ({"type": "none"}, None),
    ],
)
def test_anthropic_tool_choice_to_converse(tool_choice, expected):
    assert anthropic_tool_choice_to_converse(tool_choice) == expected


def test_converse_response_to_anthropic_text():
    response = {
        "output": {"message": {"content": [{"text": "hi there"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    result = converse_response_to_anthropic(response, "amazon.nova-pro-v1:0")
    assert result["content"] == [{"type": "text", "text": "hi there"}]
    assert result["stop_reason"] == "end_turn"
    assert result["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_converse_response_to_anthropic_tool_use():
    response = {
        "output": {
            "message": {
                "content": [{"toolUse": {"toolUseId": "t1", "name": "search", "input": {"q": "x"}}}]
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }
    result = converse_response_to_anthropic(response, "amazon.nova-pro-v1:0")
    assert result["content"] == [
        {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}}
    ]
    assert result["stop_reason"] == "tool_use"


def test_response_namespace_blocks_serialize_losslessly():
    """Regression: content blocks from the Converse path must survive
    ``_serialize_content`` with their fields intact. Before, they were bare
    SimpleNamespaces with no ``model_dump()``, so a tool_use block collapsed to
    ``{"type": "tool_use", "raw": "namespace(...)"}`` — losing id/name/input.
    ``strip_orphaned_tool_results`` then dropped the tool_use (id=None) and its
    tool_result, so Kimi/Nova never saw the result and looped forever.
    """
    import json

    from foreman.message_utils import _serialize_content, strip_orphaned_tool_results
    from foreman.providers.bedrock import _response_dict_to_namespace

    adict = converse_response_to_anthropic(
        {
            "output": {
                "message": {
                    "content": [
                        {"text": " Let me check the task."},
                        {
                            "toolUse": {
                                "toolUseId": "tu-1",
                                "name": "get_task_status",
                                "input": {"task_id": "t-1"},
                            }
                        },
                    ]
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 10, "outputTokens": 2},
        },
        "moonshotai.kimi-k2.5",
    )
    ns = _response_dict_to_namespace(adict)

    blocks = json.loads(_serialize_content(ns.content))
    assert blocks == [
        {"type": "text", "text": " Let me check the task."},
        {"type": "tool_use", "id": "tu-1", "name": "get_task_status", "input": {"task_id": "t-1"}},
    ]

    # The tool_result must survive orphan-stripping (i.e. its tool_use is intact).
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "state"}]},
        {"role": "assistant", "content": blocks},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu-1", "content": "pending"}],
        },
    ]
    assert len(strip_orphaned_tool_results(messages)) == 3


# ---------------------------------------------------------------------------
# BedrockNativeClient.create — mocked boto3
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_client_cache():
    import foreman.providers.bedrock as bedrock_mod

    bedrock_mod._clients.clear()
    yield
    bedrock_mod._clients.clear()


async def test_bedrock_native_client_create_success(monkeypatch):
    fake_boto_client = MagicMock()
    fake_boto_client.converse.return_value = {
        "output": {"message": {"content": [{"text": "hello from nova"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 7, "outputTokens": 3},
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "req-123"},
    }

    fake_session = MagicMock()
    fake_session.client.return_value = fake_boto_client

    with patch("foreman.providers.bedrock.boto3") as fake_boto3:
        fake_boto3.Session.return_value = fake_session
        client = BedrockNativeClient(region="us-east-1", extra_env={})
        raw = await client.messages.with_raw_response.create(
            model="amazon.nova-pro-v1:0",
            max_tokens=100,
            system=[{"type": "text", "text": "be helpful"}],
            messages=[{"role": "user", "content": "hi"}],
        )

    parsed = raw.parse()
    assert parsed.content[0].text == "hello from nova"
    assert parsed.stop_reason == "end_turn"
    assert parsed.usage.input_tokens == 7
    assert parsed.usage.output_tokens == 3
    assert raw.headers.get("request-id") == "req-123"

    fake_boto_client.converse.assert_called_once()
    call_kwargs = fake_boto_client.converse.call_args.kwargs
    assert call_kwargs["modelId"] == "amazon.nova-pro-v1:0"
    assert call_kwargs["inferenceConfig"] == {"maxTokens": 100}


async def test_bedrock_native_client_uses_bearer_token(monkeypatch):
    """A bearer token in extra_env forces the bearer signer and is handed to
    the client directly — boto3's own token provider only reads os.environ,
    which never carries the guild-supplied token."""
    fake_boto_client = MagicMock()
    fake_boto_client.converse.return_value = {
        "output": {"message": {"content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1},
        "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "req-1"},
    }
    fake_session = MagicMock()
    fake_session.client.return_value = fake_boto_client

    with patch("foreman.providers.bedrock.boto3") as fake_boto3:
        fake_boto3.Session.return_value = fake_session
        client = BedrockNativeClient(
            region="us-east-1", extra_env={"AWS_BEARER_TOKEN_BEDROCK": "tok-abc"}
        )
        await client.messages.with_raw_response.create(
            model="moonshotai.kimi-k2.5",
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
        )

    # Client built with the bearer signature version, and the token injected.
    config = fake_session.client.call_args.kwargs["config"]
    assert config.signature_version == "bearer"
    assert fake_boto_client._request_signer._auth_token.token == "tok-abc"
    # No AWS keys/profile passed on the session when using a bearer token.
    assert fake_boto3.Session.call_args.kwargs == {"region_name": "us-east-1"}


async def test_bedrock_native_client_create_without_boto3_raises():
    with (
        patch("foreman.providers.bedrock.HAS_BOTO3", False),
        patch("foreman.providers.bedrock.boto3", None),
    ):
        client = BedrockNativeClient(region="us-east-1", extra_env={})
        with pytest.raises(ImportError):
            await client.messages.with_raw_response.create(
                model="amazon.nova-pro-v1:0",
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
            )
