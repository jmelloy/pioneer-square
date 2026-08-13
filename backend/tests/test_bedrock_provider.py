"""Tests for the Bedrock Converse API provider wrapper (Amazon Nova, Kimi K2)."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman.providers.bedrock import (
    BedrockNativeClient,
    BedrockResponsesClient,
    anthropic_messages_to_converse,
    anthropic_messages_to_responses_input,
    anthropic_tool_choice_to_converse,
    anthropic_tool_choice_to_responses,
    anthropic_tools_to_converse,
    anthropic_tools_to_responses,
    converse_response_to_anthropic,
    is_native_bedrock_model,
    is_responses_api_model,
    responses_output_to_anthropic,
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


def test_converse_response_to_anthropic_strips_kimi_think_block():
    response = {
        "output": {
            "message": {
                "content": [
                    {
                        "text": "<think>\nSome reasoning here...\n</think>\nActual generated content here"
                    }
                ]
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    result = converse_response_to_anthropic(response, "moonshotai.kimi-k2-instruct")
    assert result["content"] == [{"type": "text", "text": "Actual generated content here"}]


def test_converse_response_to_anthropic_strips_multiple_kimi_think_blocks():
    response = {
        "output": {
            "message": {
                "content": [
                    {"text": "<think>first</think>middle<think>second</think>end"},
                ]
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }
    result = converse_response_to_anthropic(response, "moonshot.kimi-k2-instruct")
    assert result["content"] == [{"type": "text", "text": "middleend"}]


def test_converse_response_to_anthropic_leaves_non_kimi_text_untouched():
    """A <think> tag literally present in a non-Kimi model's own output must
    survive untouched — the strip is scoped to Kimi models only."""
    response = {
        "output": {"message": {"content": [{"text": "<think>not reasoning</think>real text"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }
    result = converse_response_to_anthropic(response, "amazon.nova-pro-v1:0")
    assert result["content"] == [{"type": "text", "text": "<think>not reasoning</think>real text"}]


def test_converse_response_to_anthropic_strips_unpaired_kimi_think_close():
    """Real Bedrock responses sometimes omit the opening <think> tag while
    still emitting the closing </think> — everything before it is reasoning
    text that must still be stripped."""
    response = {
        "output": {
            "message": {
                "content": [
                    {
                        "text": "Let me claim the issue and assign it to w-d04281.  </think>"
                        "            Claiming the issue and assigning **w-d04281**"
                    }
                ]
            }
        },
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    result = converse_response_to_anthropic(response, "moonshotai.kimi-k2-instruct")
    assert result["content"] == [
        {"type": "text", "text": "Claiming the issue and assigning **w-d04281**"}
    ]


def test_converse_response_to_anthropic_kimi_no_think_block_untouched():
    response = {
        "output": {"message": {"content": [{"text": "plain answer, no reasoning tag"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1},
    }
    result = converse_response_to_anthropic(response, "moonshotai.kimi-k2-instruct")
    assert result["content"] == [{"type": "text", "text": "plain answer, no reasoning tag"}]


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


# ---------------------------------------------------------------------------
# is_responses_api_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "openai.gpt-oss-120b",
        "openai.gpt-oss-20b",
        "us.openai.gpt-oss-120b",
        "OPENAI.GPT-OSS-20B",
    ],
)
def test_is_responses_api_model_true(model):
    assert is_responses_api_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "amazon.nova-pro-v1:0",
        "moonshotai.kimi-k2-instruct",
        "anthropic.claude-sonnet-4-6-20251001-v1:0",
        "openai.gpt-oss-safeguard-120b",
        "openai.gpt-oss-safeguard-20b",
        "claude-sonnet-4-6",
    ],
)
def test_is_responses_api_model_false(model):
    assert is_responses_api_model(model) is False


# ---------------------------------------------------------------------------
# Anthropic <-> Responses API translation
# ---------------------------------------------------------------------------


def test_anthropic_messages_to_responses_input_text_only():
    items = anthropic_messages_to_responses_input(
        [{"type": "text", "text": "be helpful"}],
        [{"role": "user", "content": "hello"}],
    )
    assert items == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hello"},
    ]


def test_anthropic_messages_to_responses_input_tool_use_and_result():
    items = anthropic_messages_to_responses_input(
        None,
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "result"}],
            },
        ],
    )
    assert items == [
        {"role": "assistant", "content": "let me check"},
        {"type": "function_call", "call_id": "t1", "name": "search", "arguments": '{"q": "x"}'},
        {"type": "function_call_output", "call_id": "t1", "output": "result"},
    ]


def test_anthropic_tools_to_responses():
    tools = anthropic_tools_to_responses(
        [{"name": "search", "description": "search the web", "input_schema": {"type": "object"}}]
    )
    assert tools == [
        {
            "type": "function",
            "name": "search",
            "description": "search the web",
            "parameters": {"type": "object"},
        }
    ]


@pytest.mark.parametrize(
    "tool_choice,expected",
    [
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "none"}, "none"),
        ({"type": "tool", "name": "search"}, {"type": "function", "name": "search"}),
        (None, None),
    ],
)
def test_anthropic_tool_choice_to_responses(tool_choice, expected):
    assert anthropic_tool_choice_to_responses(tool_choice) == expected


def test_responses_output_to_anthropic_text():
    response = {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hi there"}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result = responses_output_to_anthropic(response, "openai.gpt-oss-120b")
    assert result["content"] == [{"type": "text", "text": "hi there"}]
    assert result["stop_reason"] == "end_turn"
    assert result["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_responses_output_to_anthropic_function_call():
    response = {
        "id": "resp_2",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"q": "x"}',
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    result = responses_output_to_anthropic(response, "openai.gpt-oss-120b")
    assert result["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "x"}}
    ]
    assert result["stop_reason"] == "tool_use"


def test_responses_output_to_anthropic_incomplete_max_tokens():
    response = {
        "id": "resp_3",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "cut off"}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    result = responses_output_to_anthropic(response, "openai.gpt-oss-120b")
    assert result["stop_reason"] == "max_tokens"


# ---------------------------------------------------------------------------
# BedrockResponsesClient.create — mocked httpx
# ---------------------------------------------------------------------------


async def test_bedrock_responses_client_create_success_bearer_token():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.headers = {"x-request-id": "req-abc"}
    fake_response.json.return_value = {
        "id": "resp_1",
        "status": "completed",
        "model": "openai.gpt-oss-120b",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello from gpt-oss"}],
            }
        ],
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }
    fake_response.raise_for_status = MagicMock()

    fake_http_client = MagicMock()
    fake_http_client.post = AsyncMock(return_value=fake_response)
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch("foreman.providers.bedrock.httpx.AsyncClient", return_value=fake_http_client):
        client = BedrockResponsesClient(
            region="us-east-1", extra_env={"AWS_BEARER_TOKEN_BEDROCK": "tok-abc"}
        )
        raw = await client.messages.with_raw_response.create(
            model="openai.gpt-oss-120b",
            max_tokens=100,
            system=[{"type": "text", "text": "be helpful"}],
            messages=[{"role": "user", "content": "hi"}],
        )

    parsed = raw.parse()
    assert parsed.content[0].text == "hello from gpt-oss"
    assert parsed.stop_reason == "end_turn"
    assert parsed.usage.input_tokens == 7
    assert parsed.usage.output_tokens == 3
    assert raw.headers.get("request-id") == "req-abc"

    fake_http_client.post.assert_called_once()
    call_args = fake_http_client.post.call_args
    assert call_args.args[0] == "https://bedrock-mantle.us-east-1.api.aws/v1/responses"
    body = call_args.kwargs["json"]
    assert body["model"] == "openai.gpt-oss-120b"
    assert body["max_output_tokens"] == 100
    assert body["store"] is False
    assert body["input"][0] == {"role": "system", "content": "be helpful"}
    headers = call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-abc"


async def test_bedrock_responses_client_includes_tools():
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.headers = {}
    fake_response.json.return_value = {
        "id": "resp_2",
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    fake_response.raise_for_status = MagicMock()

    fake_http_client = MagicMock()
    fake_http_client.post = AsyncMock(return_value=fake_response)
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)

    with patch("foreman.providers.bedrock.httpx.AsyncClient", return_value=fake_http_client):
        client = BedrockResponsesClient(
            region="us-east-1", extra_env={"AWS_BEARER_TOKEN_BEDROCK": "tok-abc"}
        )
        await client.messages.with_raw_response.create(
            model="openai.gpt-oss-120b",
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"name": "search", "description": "search", "input_schema": {}}],
            tool_choice={"type": "auto"},
        )

    body = fake_http_client.post.call_args.kwargs["json"]
    assert body["tools"] == [
        {"type": "function", "name": "search", "description": "search", "parameters": {}}
    ]
    assert body["tool_choice"] == "auto"


async def test_bedrock_responses_client_sigv4_when_no_bearer_token():
    """Without a bearer token, the request must be SigV4-signed via botocore
    rather than sent unauthenticated."""
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.headers = {}
    fake_response.json.return_value = {
        "id": "resp_3",
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    fake_response.raise_for_status = MagicMock()

    fake_http_client = MagicMock()
    fake_http_client.post = AsyncMock(return_value=fake_response)
    fake_http_client.__aenter__ = AsyncMock(return_value=fake_http_client)
    fake_http_client.__aexit__ = AsyncMock(return_value=False)

    fake_credentials = MagicMock()
    fake_session = MagicMock()
    fake_session.get_credentials.return_value = fake_credentials

    with (
        patch("foreman.providers.bedrock.httpx.AsyncClient", return_value=fake_http_client),
        patch("foreman.providers.bedrock.boto3") as fake_boto3,
        patch("botocore.auth.SigV4Auth.add_auth") as fake_add_auth,
    ):
        fake_boto3.Session.return_value = fake_session

        def _sign(request):
            request.headers["Authorization"] = "AWS4-HMAC-SHA256 Credential=fake"

        fake_add_auth.side_effect = _sign

        client = BedrockResponsesClient(region="us-east-1", extra_env={})
        await client.messages.with_raw_response.create(
            model="openai.gpt-oss-120b",
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
        )

    headers = fake_http_client.post.call_args.kwargs["headers"]
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    fake_add_auth.assert_called_once()


async def test_bedrock_responses_client_no_credentials_raises():
    fake_session = MagicMock()
    fake_session.get_credentials.return_value = None

    with patch("foreman.providers.bedrock.boto3") as fake_boto3:
        fake_boto3.Session.return_value = fake_session
        client = BedrockResponsesClient(region="us-east-1", extra_env={})
        with pytest.raises(ValueError):
            await client.messages.with_raw_response.create(
                model="openai.gpt-oss-120b",
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
            )
