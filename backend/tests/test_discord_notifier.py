"""Unit tests for discord_notifier.

No real HTTP requests are made — the httpx client is mocked throughout.
No real DB sessions are made — _lookup_thread and _save_thread are patched.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discord_notifier

WEBHOOK_URL = "https://discord.com/api/webhooks/test/token"
BOT_TOKEN = "Bot.test.token"
CHANNEL_ID = "111222333444"
THREAD_ID = "555666777888"


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Start each test with no tokens set."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)


@pytest.fixture(autouse=True)
def reset_client():
    """Reset the module-level httpx singleton so each test gets a fresh client."""
    discord_notifier._client = None
    yield
    discord_notifier._client = None


def _make_mock_client(status_code: int = 204, side_effect=None, json_response: dict | None = None):
    """Return a mock httpx.AsyncClient with stubbed HTTP methods."""
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = status_code
    mock_response.content = json.dumps(json_response).encode() if json_response else b""
    mock_response.json = MagicMock(return_value=json_response or {})
    if status_code >= 400:
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock_response
        )
    else:
        mock_response.raise_for_status.return_value = None

    mock_client = MagicMock(spec=httpx.AsyncClient)
    if side_effect is not None:
        mock_client.post = AsyncMock(side_effect=side_effect)
        mock_client.patch = AsyncMock(side_effect=side_effect)
        mock_client.get = AsyncMock(side_effect=side_effect)
    else:
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.patch = AsyncMock(return_value=mock_response)
        mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# Phase 1: flat webhook (unchanged behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_no_op_when_env_unset():
    """notify() must not make any HTTP call when DISCORD_WEBHOOK_URL is unset."""
    mock_client = _make_mock_client()
    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("task-complete", "Done", "All good")
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_posts_correct_embed(monkeypatch):
    """notify() builds the right embed payload and POSTs it to the webhook."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify(
            "pr-opened", "PR #42 opened", "New pull request", url="https://example.com/pr/42"
        )

    mock_client.post.assert_called_once()
    _, kwargs = mock_client.post.call_args
    body = kwargs["json"]
    assert body["embeds"][0]["title"] == "PR #42 opened"
    assert body["embeds"][0]["description"] == "New pull request"
    assert body["embeds"][0]["url"] == "https://example.com/pr/42"
    assert body["embeds"][0]["color"] == 0x3498DB  # blue


@pytest.mark.asyncio
async def test_notify_no_url_field_when_omitted(monkeypatch):
    """url field must be absent from the embed when url=None."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("worker-online", "Worker connected", "w-abc is online")

    _, kwargs = mock_client.post.call_args
    assert "url" not in kwargs["json"]["embeds"][0]


# ---------------------------------------------------------------------------
# Colour map
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type,expected_color",
    [
        ("task-complete", 0x2ECC71),
        ("task-failed", 0xE74C3C),
        ("task-cancelled", 0xE74C3C),
        ("pr-opened", 0x3498DB),
        ("pr-merged", 0x9B59B6),
        ("pr-closed", 0x95A5A6),
        ("worker-online", 0x1ABC9C),
        ("worker-offline", 0xE67E22),
        ("ci-pass", 0x2ECC71),
        ("ci-fail", 0xE74C3C),
        ("unknown-event", 0x7289DA),  # default blurple
    ],
)
async def test_colour_by_event_type(monkeypatch, event_type, expected_color):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify(event_type, "title", "desc")

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["embeds"][0]["color"] == expected_color


@pytest.mark.asyncio
async def test_custom_color_overrides_event_type(monkeypatch):
    """Explicit color= kwarg takes precedence over the event-type colour map."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("task-complete", "t", "d", color=0xABCDEF)

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["embeds"][0]["color"] == 0xABCDEF


# ---------------------------------------------------------------------------
# HTTP failure must not raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_does_not_raise(monkeypatch):
    """An HTTP error must be swallowed and logged, never propagated."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    mock_client = _make_mock_client(status_code=500)

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("task-failed", "Failed", "oh no")


@pytest.mark.asyncio
async def test_network_error_does_not_raise(monkeypatch):
    """A network-level exception (connection refused etc.) must be swallowed."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    mock_client = _make_mock_client(side_effect=httpx.ConnectError("refused"))

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("task-failed", "Failed", "network down")


# ---------------------------------------------------------------------------
# Phase 2: thread creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_thread_creates_new_thread(monkeypatch):
    """_ensure_thread creates a Discord thread when none exists in DB.

    Thread creation for a standard text channel is a two-step process:
    1. POST a starter message to get a message_id.
    2. POST to /messages/{message_id}/threads to create the thread.
    """
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    MSG_ID = "msg_starter_123"

    msg_response = MagicMock(spec=httpx.Response)
    msg_response.status_code = 200
    msg_response.content = json.dumps({"id": MSG_ID}).encode()
    msg_response.json = MagicMock(return_value={"id": MSG_ID})
    msg_response.raise_for_status.return_value = None

    thread_response = MagicMock(spec=httpx.Response)
    thread_response.status_code = 200
    thread_response.content = json.dumps({"id": THREAD_ID, "name": "#42: Fix"}).encode()
    thread_response.json = MagicMock(return_value={"id": THREAD_ID, "name": "#42: Fix"})
    thread_response.raise_for_status.return_value = None

    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=[msg_response, thread_response])
    mock_client.patch = AsyncMock()
    mock_client.get = AsyncMock()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)),
        patch.object(discord_notifier, "_save_thread", AsyncMock()),
    ):
        result = await discord_notifier._ensure_thread("org/repo", 42, "#42: Fix")

    assert result == THREAD_ID
    assert mock_client.post.call_count == 2

    # First call: starter message posted in the channel
    first_call = mock_client.post.call_args_list[0]
    assert f"/channels/{CHANNEL_ID}/messages" in first_call[0][0]

    # Second call: thread created from the starter message
    second_call = mock_client.post.call_args_list[1]
    assert f"/channels/{CHANNEL_ID}/messages/{MSG_ID}/threads" in second_call[0][0]
    body = second_call[1]["json"]
    assert body["name"] == "#42: Fix"


@pytest.mark.asyncio
async def test_ensure_thread_reuses_existing(monkeypatch):
    """_ensure_thread returns persisted thread_id without calling Discord API."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=THREAD_ID)),
        patch.object(discord_notifier, "_save_thread", AsyncMock()),
    ):
        result = await discord_notifier._ensure_thread("org/repo", 42, "#42: Fix")

    assert result == THREAD_ID
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_thread_no_channel_id_returns_none(monkeypatch):
    """_ensure_thread returns None when DISCORD_CHANNEL_ID is not set."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    # no DISCORD_CHANNEL_ID

    with patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)):
        result = await discord_notifier._ensure_thread("org/repo", 42, "#42: Fix")

    assert result is None


@pytest.mark.asyncio
async def test_ensure_thread_no_bot_token_returns_none(monkeypatch):
    """_ensure_thread returns None when DISCORD_BOT_TOKEN is not set."""
    # no BOT_TOKEN, no CHANNEL_ID
    with patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)):
        result = await discord_notifier._ensure_thread("org/repo", 42, "#42: Fix")

    assert result is None


@pytest.mark.asyncio
async def test_ensure_thread_api_error_returns_none(monkeypatch):
    """_ensure_thread returns None on Discord API error — never raises."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=500)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)),
        patch.object(discord_notifier, "_save_thread", AsyncMock()),
    ):
        result = await discord_notifier._ensure_thread("org/repo", 42, "#42: Fix")

    assert result is None


@pytest.mark.asyncio
async def test_thread_name_truncated_to_100_chars(monkeypatch):
    """Thread name is capped at 100 characters per Discord limits."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    long_name = "x" * 200
    MSG_ID = "msg_starter_456"

    msg_response = MagicMock(spec=httpx.Response)
    msg_response.status_code = 200
    msg_response.content = json.dumps({"id": MSG_ID}).encode()
    msg_response.json = MagicMock(return_value={"id": MSG_ID})
    msg_response.raise_for_status.return_value = None

    thread_response = MagicMock(spec=httpx.Response)
    thread_response.status_code = 200
    thread_response.content = json.dumps({"id": THREAD_ID}).encode()
    thread_response.json = MagicMock(return_value={"id": THREAD_ID})
    thread_response.raise_for_status.return_value = None

    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=[msg_response, thread_response])
    mock_client.patch = AsyncMock()
    mock_client.get = AsyncMock()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)),
        patch.object(discord_notifier, "_save_thread", AsyncMock()),
    ):
        await discord_notifier._ensure_thread("org/repo", 1, long_name)

    # Thread creation is the second POST call — name must be capped
    second_call = mock_client.post.call_args_list[1]
    assert len(second_call[1]["json"]["name"]) <= 100


# ---------------------------------------------------------------------------
# Phase 2: thread routing via notify_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_event_routes_to_thread_when_bot_configured(monkeypatch):
    """notify_event posts to a thread when bot token + repo/number are provided."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_event(
            "pr-opened",
            title="PR #42 opened",
            description="New PR",
            url="https://github.com/org/repo/pull/42",
            issue_repo="org/repo",
            issue_number=42,
        )

    # Should POST to the thread messages endpoint, not the webhook URL
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{THREAD_ID}/messages" in call_url


@pytest.mark.asyncio
async def test_notify_event_posts_header_fields_as_embed_fields(monkeypatch):
    """header_fields are serialised into Discord embed fields."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)
    fields = {"Assignee": "@alice", "Labels": "bug, critical"}

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_event(
            "pr-opened",
            title="PR #42",
            description="desc",
            issue_repo="org/repo",
            issue_number=42,
            header_fields=fields,
        )

    _, kwargs = mock_client.post.call_args
    embed_fields = kwargs["json"]["embeds"][0]["fields"]
    field_names = {f["name"] for f in embed_fields}
    assert "Assignee" in field_names
    assert "Labels" in field_names


@pytest.mark.asyncio
async def test_notify_event_archives_thread_on_close(monkeypatch):
    """notify_event archives the thread when close=True."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_event(
            "pr-merged",
            title="PR merged",
            description="Merged!",
            issue_repo="org/repo",
            issue_number=42,
            close=True,
        )

    # Two calls: POST message + PATCH archive
    assert mock_client.post.call_count == 1
    assert mock_client.patch.call_count == 1
    patch_url = mock_client.patch.call_args[0][0]
    assert f"/channels/{THREAD_ID}" in patch_url
    patch_body = mock_client.patch.call_args[1]["json"]
    assert patch_body["archived"] is True


# ---------------------------------------------------------------------------
# Phase 2: graceful fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_event_falls_back_to_webhook_when_no_bot_token(monkeypatch):
    """notify_event falls back to flat webhook when DISCORD_BOT_TOKEN is absent."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)
    # no bot token

    mock_client = _make_mock_client(status_code=204)

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify_event(
            "pr-opened",
            title="PR opened",
            description="desc",
            url="https://example.com/pr/1",
            issue_repo="org/repo",
            issue_number=1,
        )

    # Should use the webhook URL, not a bot API endpoint
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert call_url == WEBHOOK_URL


@pytest.mark.asyncio
async def test_notify_event_falls_back_when_thread_creation_fails(monkeypatch):
    """notify_event falls back to flat webhook when thread creation returns None."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=None)),
    ):
        await discord_notifier.notify_event(
            "pr-opened",
            title="PR opened",
            description="desc",
            issue_repo="org/repo",
            issue_number=1,
        )

    # Should use the webhook URL as fallback
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert call_url == WEBHOOK_URL


@pytest.mark.asyncio
async def test_notify_event_silent_noop_when_no_tokens(monkeypatch):
    """notify_event is a no-op when neither bot token nor webhook are set."""
    # no bot token, no webhook URL

    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=None)),
    ):
        await discord_notifier.notify_event(
            "pr-opened",
            title="PR opened",
            description="desc",
            issue_repo="org/repo",
            issue_number=1,
        )

    mock_client.post.assert_not_called()
    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_notify_event_without_issue_coords_uses_webhook(monkeypatch):
    """notify_event without repo/number falls back to flat webhook (no thread attempt)."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)

    mock_client = _make_mock_client(status_code=204)

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify_event(
            "worker-online",
            title="Worker online",
            description="w-abc connected",
        )

    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert call_url == WEBHOOK_URL


# ---------------------------------------------------------------------------
# Phase 2: archive_thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_thread_patches_channel(monkeypatch):
    """archive_thread sends PATCH /channels/{thread_id} with archived=true."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    mock_client = _make_mock_client(status_code=200, json_response={})

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.archive_thread(THREAD_ID)

    mock_client.patch.assert_called_once()
    url, kwargs = mock_client.patch.call_args[0][0], mock_client.patch.call_args[1]
    assert f"/channels/{THREAD_ID}" in url
    assert kwargs["json"]["archived"] is True


@pytest.mark.asyncio
async def test_archive_thread_no_bot_token_is_noop(monkeypatch):
    """archive_thread is a no-op when bot token is absent."""
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.archive_thread(THREAD_ID)

    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_archive_thread_http_error_does_not_raise(monkeypatch):
    """archive_thread swallows HTTP errors — never propagates."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    mock_client = _make_mock_client(status_code=500)

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.archive_thread(THREAD_ID)
