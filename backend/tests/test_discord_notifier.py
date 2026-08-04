"""Unit tests for discord_notifier.

No real HTTP requests are made — the httpx client is mocked throughout.
DB access is patched out (_lookup_thread / _save_thread) except in the few
tests that take the ``client`` fixture to exercise a real query.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from datetime import UTC, datetime

import discord_notifier
from helpers import _sync_session, insert_guild, insert_task, make_auth_token
from models import GithubPullRequest
from sqlalchemy import insert as sa_insert


def _insert_gh_pr(db_url: str, repo: str, number: int, *, body: str | None = None) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            sa_insert(GithubPullRequest).values(
                repo=repo,
                number=number,
                title="",
                state="open",
                body=body,
                merged=False,
                draft=False,
                assignees=[],
                labels=[],
                created_at=now,
                updated_at=now,
                raw={},
            )
        )
        session.commit()


BOT_TOKEN = "Bot.test.token"
CHANNEL_ID = "111222333444"
THREAD_ID = "555666777888"


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Start each test with no tokens set."""
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DISCORD_STREAM_TASKS", raising=False)


@pytest.fixture(autouse=True)
def reset_client():
    """Reset the module-level httpx singleton so each test gets a fresh client."""
    discord_notifier._client = None
    yield
    discord_notifier._client = None


@pytest.fixture(autouse=True)
def reset_stream_buffers():
    """Clear the in-memory task-stream buffers between tests."""
    discord_notifier._stream_buffers.clear()
    yield
    discord_notifier._stream_buffers.clear()


@pytest.fixture(autouse=True)
def reset_ci_batches():
    """Clear the in-memory CI check batches between tests."""
    discord_notifier._ci_batches.clear()
    yield
    discord_notifier._ci_batches.clear()


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
# Flat-channel notification (posted via the bot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_no_op_when_bot_token_unset():
    """notify() must not make any HTTP call when DISCORD_BOT_TOKEN is unset."""
    mock_client = _make_mock_client()
    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("task-complete", "Done", "All good")
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_no_op_when_channel_unset(monkeypatch):
    """notify() must not post when a destination channel cannot be resolved."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    # no DISCORD_CHANNEL_ID
    mock_client = _make_mock_client()
    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("task-complete", "Done", "All good")
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_posts_correct_embed(monkeypatch):
    """notify() builds the right embed payload and POSTs it to the channel via the bot."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify(
            "pr-opened", "PR #42 opened", "New pull request", url="https://example.com/pr/42"
        )

    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{CHANNEL_ID}/messages" in call_url
    body = mock_client.post.call_args[1]["json"]
    assert body["embeds"][0]["title"] == "PR #42 opened"
    assert body["embeds"][0]["description"] == "New pull request"
    assert body["embeds"][0]["url"] == "https://example.com/pr/42"
    assert body["embeds"][0]["color"] == 0x3498DB  # blue


@pytest.mark.asyncio
async def test_notify_no_url_field_when_omitted(monkeypatch):
    """url field must be absent from the embed when url=None."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
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
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify(event_type, "title", "desc")

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["embeds"][0]["color"] == expected_color


@pytest.mark.asyncio
async def test_custom_color_overrides_event_type(monkeypatch):
    """Explicit color= kwarg takes precedence over the event-type colour map."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
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
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client(status_code=500)

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify("task-failed", "Failed", "oh no")


@pytest.mark.asyncio
async def test_network_error_does_not_raise(monkeypatch):
    """A network-level exception (connection refused etc.) must be swallowed."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
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
# Graceful fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_event_noop_when_no_bot_token(monkeypatch):
    """notify_event is a no-op when DISCORD_BOT_TOKEN is absent."""
    # no bot token — the flat-channel fallback also runs through the bot, so
    # without a token there is nothing to post through.
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

    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_event_falls_back_to_channel_when_thread_creation_fails(monkeypatch):
    """notify_event falls back to a flat channel embed when thread creation returns None."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

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

    # Should post a flat embed to the configured channel via the bot
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{CHANNEL_ID}/messages" in call_url


@pytest.mark.asyncio
async def test_notify_event_routes_pr_into_linked_issue_thread(monkeypatch):
    """A PR event whose canonical key resolves to its linked issue posts (and
    creates, if needed) the ISSUE's thread — named after the issue — never a
    separate PR-numbered thread."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)
    issue_thread_id = "111222333444"

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier, "_canonical_coords", AsyncMock(return_value=("org/repo", 7))
        ),
        patch.object(
            discord_notifier, "_issue_thread_name", AsyncMock(return_value="#7: The issue")
        ),
        patch.object(
            discord_notifier, "_ensure_thread", AsyncMock(return_value=issue_thread_id)
        ) as ensure_mock,
    ):
        await discord_notifier.notify_event(
            "pr-opened",
            title="PR opened: org/repo#42",
            description="New PR",
            issue_repo="org/repo",
            issue_number=42,
            kind="pr",
        )

    # The thread was ensured under the ISSUE's coordinates, named after the issue...
    ensure_mock.assert_called_once()
    args = ensure_mock.call_args[0]
    assert args[:3] == ("org/repo", 7, "#7: The issue")
    # ...and the message posted into it.
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{issue_thread_id}/messages" in call_url


@pytest.mark.asyncio
async def test_notify_event_falls_back_to_pr_thread_when_no_linked_issue(monkeypatch):
    """A PR with no resolvable linked issue keys its thread by the PR number."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)
    pr_thread_id = "555666777888"

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier, "_canonical_coords", AsyncMock(return_value=("org/repo", 42))
        ),
        patch.object(
            discord_notifier, "_ensure_thread", AsyncMock(return_value=pr_thread_id)
        ) as ensure_mock,
    ):
        await discord_notifier.notify_event(
            "pr-opened",
            title="PR opened: org/repo#42",
            description="New PR",
            issue_repo="org/repo",
            issue_number=42,
            kind="pr",
        )

    ensure_mock.assert_called_once()
    assert ensure_mock.call_args[0][:2] == ("org/repo", 42)
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{pr_thread_id}/messages" in call_url


@pytest.mark.asyncio
async def test_notify_event_ignores_close_when_routed_to_linked_issue_thread(monkeypatch):
    """close=True must not archive the issue's thread — only a PR-owned thread."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)
    issue_thread_id = "111222333444"

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier, "_canonical_coords", AsyncMock(return_value=("org/repo", 7))
        ),
        patch.object(discord_notifier, "_issue_thread_name", AsyncMock(return_value=None)),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=issue_thread_id)),
    ):
        await discord_notifier.notify_event(
            "pr-merged",
            title="PR merged",
            description="Merged!",
            issue_repo="org/repo",
            issue_number=42,
            kind="pr",
            close=True,
        )

    mock_client.post.assert_called_once()
    mock_client.patch.assert_not_called()


@pytest.mark.asyncio
async def test_notify_event_archives_pr_thread_on_close(monkeypatch):
    """close=True archives the thread when the subject IS the canonical key
    (a PR with no linked issue owns its thread)."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier, "_canonical_coords", AsyncMock(return_value=("org/repo", 42))
        ),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_event(
            "pr-closed",
            title="PR closed",
            description="Closed.",
            issue_repo="org/repo",
            issue_number=42,
            kind="pr",
            close=True,
        )

    mock_client.post.assert_called_once()
    mock_client.patch.assert_called_once()


@pytest.mark.asyncio
async def test_canonical_coords_prefers_task_issue_linkage(client):
    """_canonical_coords resolves a task's issue linkage ahead of the PR subject."""
    _test_client, db_url = client
    insert_guild(db_url, "g-coords1")
    insert_task(
        db_url,
        "g-coords1",
        "t-coords1",
        state="working",
        issue_repo="org/repo",
        issue_number=7,
        pr_repo="org/repo",
        pr_number=42,
    )

    coords = await discord_notifier._canonical_coords(
        "org/repo", 42, kind="pr", task_id="t-coords1"
    )
    assert coords == ("org/repo", 7)


@pytest.mark.asyncio
async def test_canonical_coords_resolves_issue_from_cached_pr_body(client):
    """With no task linkage, the cached PR body's Closes-reference wins."""
    _test_client, db_url = client
    _insert_gh_pr(db_url, "org/coords-repo", 43, body="Fixes #9")

    coords = await discord_notifier._canonical_coords("org/coords-repo", 43, kind="pr")
    assert coords == ("org/coords-repo", 9)


@pytest.mark.asyncio
async def test_canonical_coords_issue_subject_passes_through(client):
    """An issue subject is already canonical — returned as-is."""
    coords = await discord_notifier._canonical_coords("org/repo", 5, kind="issue")
    assert coords == ("org/repo", 5)


@pytest.mark.asyncio
async def test_notify_existing_thread_posts_when_thread_found(monkeypatch):
    """notify_existing_thread posts to the thread when one is already persisted."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_existing_thread(
            "task-followup",
            title="Follow-up sent",
            description="desc",
            issue_repo="org/repo",
            issue_number=42,
        )

    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{THREAD_ID}/messages" in call_url


@pytest.mark.asyncio
async def test_notify_existing_thread_never_creates_new_thread(monkeypatch):
    """notify_existing_thread must not call thread-creation machinery."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)),
        patch.object(
            discord_notifier, "_create_thread_in_channel", AsyncMock(return_value="new-thread")
        ) as create_mock,
    ):
        await discord_notifier.notify_existing_thread(
            "task-followup",
            title="Follow-up sent",
            description="desc",
            issue_repo="org/repo",
            issue_number=42,
        )

    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_notify_existing_thread_falls_back_to_channel_when_no_thread(monkeypatch):
    """notify_existing_thread falls back to a flat channel embed when no thread is persisted."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)),
    ):
        await discord_notifier.notify_existing_thread(
            "task-redirect",
            title="Task redirected",
            description="desc",
            issue_repo="org/repo",
            issue_number=42,
        )

    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{CHANNEL_ID}/messages" in call_url


@pytest.mark.asyncio
async def test_notify_existing_thread_without_issue_coords_uses_channel(monkeypatch):
    """notify_existing_thread without repo/number skips the lookup and posts to the channel."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock()) as lookup_mock,
    ):
        await discord_notifier.notify_existing_thread(
            "task-redirect",
            title="Task redirected",
            description="desc",
        )

    lookup_mock.assert_not_called()
    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{CHANNEL_ID}/messages" in call_url


@pytest.mark.asyncio
async def test_notify_event_silent_noop_when_no_bot_token(monkeypatch):
    """notify_event is a no-op when the bot token is not set."""
    # no bot token

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
async def test_notify_event_without_issue_coords_uses_channel(monkeypatch):
    """notify_event without repo/number falls back to a flat channel embed (no thread attempt)."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client(status_code=204)

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify_event(
            "worker-online",
            title="Worker online",
            description="w-abc connected",
        )

    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{CHANNEL_ID}/messages" in call_url


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


# ---------------------------------------------------------------------------
# New-member welcome DM
# ---------------------------------------------------------------------------


def _json_response(payload: dict, status_code: int = 200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.content = json.dumps(payload).encode()
    resp.json = MagicMock(return_value=payload)
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.mark.asyncio
async def test_send_welcome_dm_opens_channel_and_posts_message(monkeypatch):
    """send_welcome_dm opens a DM channel then posts the welcome text there."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    DM_CHANNEL_ID = "dm-channel-999"

    channel_response = _json_response({"id": DM_CHANNEL_ID})
    message_response = _json_response({"id": "msg-1"})

    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=[channel_response, message_response])

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.send_welcome_dm("u123", "newperson")

    assert mock_client.post.call_count == 2

    open_call = mock_client.post.call_args_list[0]
    assert "/users/@me/channels" in open_call[0][0]
    assert open_call[1]["json"] == {"recipient_id": "u123"}

    send_call = mock_client.post.call_args_list[1]
    assert f"/channels/{DM_CHANNEL_ID}/messages" in send_call[0][0]
    assert "/connect-account" in send_call[1]["json"]["content"]
    assert "newperson" in send_call[1]["json"]["content"]


@pytest.mark.asyncio
async def test_send_welcome_dm_no_op_when_bot_token_unset():
    """send_welcome_dm makes no HTTP call when DISCORD_BOT_TOKEN is unset."""
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.send_welcome_dm("u123", "newperson")

    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_send_welcome_dm_swallows_dm_disabled_error(monkeypatch):
    """A user with DMs disabled makes channel-open fail; never raises."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    mock_client = _make_mock_client(status_code=403)

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.send_welcome_dm("u123", "newperson")  # must not raise

    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_send_welcome_dm_swallows_send_failure(monkeypatch):
    """Channel opens fine but the message POST fails; never raises."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    channel_response = _json_response({"id": "dm-channel-999"})
    failure_response = _json_response({}, status_code=500)

    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=[channel_response, failure_response])

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.send_welcome_dm("u123", "newperson")  # must not raise

    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_send_welcome_dm_defaults_username_when_missing(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    channel_response = _json_response({"id": "dm-channel-999"})
    message_response = _json_response({"id": "msg-1"})

    mock_client = MagicMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=[channel_response, message_response])

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.send_welcome_dm("u123", None)

    send_call = mock_client.post.call_args_list[1]
    assert "there" in send_call[1]["json"]["content"]


def test_welcome_dm_text_uses_default_template(monkeypatch):
    monkeypatch.delenv("DISCORD_WELCOME_DM_TEXT", raising=False)
    text = discord_notifier._welcome_dm_text("alice")
    assert "alice" in text
    assert "/connect-account" in text


def test_welcome_dm_text_honours_env_override(monkeypatch):
    monkeypatch.setenv("DISCORD_WELCOME_DM_TEXT", "Hey {username}, welcome aboard!")
    assert discord_notifier._welcome_dm_text("alice") == "Hey alice, welcome aboard!"


def test_welcome_dm_text_env_override_without_placeholder(monkeypatch):
    """A custom template with no {username} placeholder is used verbatim."""
    monkeypatch.setenv("DISCORD_WELCOME_DM_TEXT", "Welcome! Run /connect-account.")
    assert discord_notifier._welcome_dm_text("alice") == "Welcome! Run /connect-account."


# ---------------------------------------------------------------------------
# Phase 3: Foreman chat threads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_foreman_chat_no_task_id_posts_directly_to_channel(monkeypatch):
    """With no task_id, notify_foreman_chat posts straight to the main channel."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify_foreman_chat("guild-1", "Spun up worker w-abc.")

    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert f"/channels/{CHANNEL_ID}/messages" in call[0][0]
    assert call[1]["json"]["content"] == "Spun up worker w-abc."


@pytest.mark.asyncio
async def test_notify_foreman_chat_no_op_when_blank(monkeypatch):
    """notify_foreman_chat is a no-op for empty/whitespace-only content."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify_foreman_chat("guild-1", "   ")

    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_foreman_chat_no_op_when_not_configured(monkeypatch):
    """notify_foreman_chat is a no-op when the bot token/channel are unset."""
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier.notify_foreman_chat("guild-1", "hello")

    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_notify_foreman_chat_task_scoped_routes_to_task_thread(monkeypatch):
    """A task-scoped message with an existing per-task thread posts there, not the main channel."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client()
    task_thread_id = "999888777666"

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier,
            "_canonical_coords",
            AsyncMock(return_value=("org/repo", 42)),
        ),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=task_thread_id)),
    ):
        await discord_notifier.notify_foreman_chat("guild-1", "Working on it.", task_id="t-abc")

    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert f"/channels/{task_thread_id}/messages" in call[0][0]
    assert call[1]["json"]["content"] == "Working on it."


@pytest.mark.asyncio
async def test_notify_foreman_chat_no_task_id_skips_task_lookup(monkeypatch):
    """A non-scoped message (no task_id) skips the per-task lookup entirely,
    posting straight to the resolved guild channel."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_canonical_coords", AsyncMock()) as coords_mock,
    ):
        await discord_notifier.notify_foreman_chat("guild-1", "General update.")

    coords_mock.assert_not_called()
    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert f"/channels/{CHANNEL_ID}/messages" in call[0][0]
    assert call[1]["json"]["content"] == "General update."


@pytest.mark.asyncio
async def test_notify_foreman_chat_task_scoped_no_thread_posts_to_main_channel(monkeypatch):
    """Task-scoped message with no per-task thread yet posts to the main
    channel — there is no dated/daily thread fallback."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier,
            "_canonical_coords",
            AsyncMock(return_value=("org/repo", 42)),
        ),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=None)),
    ):
        await discord_notifier.notify_foreman_chat("guild-1", "No thread yet.", task_id="t-abc")

    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert f"/channels/{CHANNEL_ID}/messages" in call[0][0]
    assert call[1]["json"]["content"] == "No thread yet."


@pytest.mark.asyncio
async def test_notify_foreman_chat_task_with_no_linked_issue_posts_to_main_channel(monkeypatch):
    """Task-scoped message where the task has no linked issue/PR posts to the main channel."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)

    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_canonical_coords", AsyncMock(return_value=None)),
        patch.object(discord_notifier, "_lookup_thread", AsyncMock()) as lookup_thread_mock,
    ):
        await discord_notifier.notify_foreman_chat(
            "guild-1", "No linked issue.", task_id="t-standalone"
        )

    lookup_thread_mock.assert_not_called()
    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert f"/channels/{CHANNEL_ID}/messages" in call[0][0]


# ---------------------------------------------------------------------------
# Phase 3: GitHub login -> Discord user mentions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mention_or_login_returns_mention_when_mapped():
    """mention_or_login resolves a mapped GitHub login to a <@id> mention."""
    with patch.object(discord_notifier, "_lookup_discord_user", AsyncMock(return_value="999")):
        result = await discord_notifier.mention_or_login("alice")

    assert result == "<@999>"


@pytest.mark.asyncio
async def test_mention_or_login_falls_back_to_plain_login():
    """mention_or_login falls back to a plain @login string when no mapping exists."""
    with patch.object(discord_notifier, "_lookup_discord_user", AsyncMock(return_value=None)):
        result = await discord_notifier.mention_or_login("bob")

    assert result == "@bob"


@pytest.mark.asyncio
async def test_mention_or_login_empty_for_blank_login():
    """mention_or_login returns an empty string for a missing login without querying the DB."""
    with patch.object(discord_notifier, "_lookup_discord_user", AsyncMock()) as lookup_mock:
        result = await discord_notifier.mention_or_login(None)

    assert result == ""
    lookup_mock.assert_not_called()


@pytest.mark.asyncio
async def test_lookup_discord_user_db_error_returns_none():
    """_lookup_discord_user swallows DB errors and returns None (never raises)."""
    with patch("database.AsyncSessionLocal", side_effect=RuntimeError("db down")):
        result = await discord_notifier._lookup_discord_user("carol")

    assert result is None


def _link_discord_account(db_url: str, ps_user_id: str, discord_user_id: str) -> None:
    from models import DiscordAccountLink

    with _sync_session(db_url) as session:
        session.add(
            DiscordAccountLink(
                ps_user_id=ps_user_id,
                discord_user_id=discord_user_id,
                discord_username=f"dc-{discord_user_id}",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()


@pytest.mark.asyncio
async def test_lookup_discord_user_resolves_through_account_link(client):
    """A /connect-account link is reachable from the user's GitHub login."""
    _, db_url = client
    make_auth_token(db_url, user_id="gh-mention-1", username="mentionee")
    _link_discord_account(db_url, "gh-mention-1", "dc-mention-1")

    assert await discord_notifier._lookup_discord_user("mentionee") == "dc-mention-1"


@pytest.mark.asyncio
async def test_lookup_discord_user_is_case_insensitive(client):
    """users.github_login keeps GitHub's casing, so lookups must not depend on it."""
    _, db_url = client
    make_auth_token(db_url, user_id="gh-mention-2", username="MixedCase")
    _link_discord_account(db_url, "gh-mention-2", "dc-mention-2")

    assert await discord_notifier._lookup_discord_user("mixedcase") == "dc-mention-2"


@pytest.mark.asyncio
async def test_lookup_discord_user_none_when_unlinked(client):
    """A known user who never ran /connect-account has no Discord ID to mention."""
    _, db_url = client
    make_auth_token(db_url, user_id="gh-mention-3", username="unlinked")

    assert await discord_notifier._lookup_discord_user("unlinked") is None


@pytest.mark.asyncio
async def test_canonical_coords_db_error_falls_back_to_subject():
    """_canonical_coords swallows DB errors: falls back to the given subject
    coordinates, and to None when it has nothing to fall back to."""
    with patch("database.AsyncSessionLocal", side_effect=RuntimeError("db down")):
        assert await discord_notifier._canonical_coords(
            "org/repo", 42, kind="pr", task_id="t-abc"
        ) == ("org/repo", 42)
        assert await discord_notifier._canonical_coords(None, None, task_id="t-abc") is None


# ---------------------------------------------------------------------------
# Phase 4: live task-stream mirroring
# ---------------------------------------------------------------------------


def test_stream_enabled_requires_both_flag_and_token(monkeypatch):
    """_stream_enabled is off unless DISCORD_STREAM_TASKS is truthy AND a bot token is set."""
    assert discord_notifier._stream_enabled() is False

    monkeypatch.setenv("DISCORD_STREAM_TASKS", "true")
    assert discord_notifier._stream_enabled() is False  # still no bot token

    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    assert discord_notifier._stream_enabled() is True

    monkeypatch.setenv("DISCORD_STREAM_TASKS", "off")
    assert discord_notifier._stream_enabled() is False


@pytest.mark.asyncio
async def test_notify_task_stream_noop_when_disabled(monkeypatch):
    """notify_task_stream buffers nothing when the feature flag is unset."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)  # token set, flag NOT set

    await discord_notifier.notify_task_stream("guild-1", "t-1", "hello")

    assert "t-1" not in discord_notifier._stream_buffers


@pytest.mark.asyncio
async def test_notify_task_stream_noop_for_blank_line(monkeypatch):
    """notify_task_stream ignores blank/whitespace-only lines."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_STREAM_TASKS", "1")

    await discord_notifier.notify_task_stream("guild-1", "t-1", "   ")

    assert "t-1" not in discord_notifier._stream_buffers


@pytest.mark.asyncio
async def test_notify_task_stream_buffers_short_line(monkeypatch):
    """A short line is buffered and a delayed flush is armed (no immediate POST)."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_STREAM_TASKS", "1")

    with patch.object(discord_notifier, "_delayed_flush", AsyncMock()):
        await discord_notifier.notify_task_stream("guild-1", "t-1", "short line")

        buf = discord_notifier._stream_buffers["t-1"]
        assert buf.lines == [discord_notifier._StreamEntry(line="short line")]
        assert buf.guild_id == "guild-1"
        assert buf.flush_task is not None
        await buf.flush_task  # drain the scheduled (mocked) flush task


@pytest.mark.asyncio
async def test_notify_task_stream_flushes_when_buffer_large(monkeypatch):
    """Crossing _STREAM_FLUSH_CHARS triggers an immediate background flush."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_STREAM_TASKS", "1")
    monkeypatch.setattr(discord_notifier, "_STREAM_FLUSH_CHARS", 10)

    with patch.object(discord_notifier, "_flush_task_stream", AsyncMock()) as flush_mock:
        await discord_notifier.notify_task_stream("guild-1", "t-1", "a line much longer than ten")
        await asyncio.sleep(0)  # let the scheduled flush task run

    flush_mock.assert_awaited_once_with("t-1")


@pytest.mark.asyncio
async def test_post_task_stream_posts_silent_message_to_task_thread(monkeypatch):
    """_post_task_stream posts into the per-task thread as a silent, no-mention message."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)

    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier, "_resolve_channel_for_guild", AsyncMock(return_value=CHANNEL_ID)
        ),
        patch.object(discord_notifier, "_ensure_task_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier._post_task_stream("guild-1", "t-1", "line one\nline two")

    mock_client.post.assert_called_once()
    call_url = mock_client.post.call_args[0][0]
    assert f"/channels/{THREAD_ID}/messages" in call_url
    body = mock_client.post.call_args[1]["json"]
    assert body["content"] == "line one\nline two"
    assert body["flags"] == discord_notifier._SILENT_FLAG
    assert body["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_post_task_stream_noop_when_no_thread(monkeypatch):
    """_post_task_stream posts nothing when the task thread can't be created."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)

    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(
            discord_notifier, "_resolve_channel_for_guild", AsyncMock(return_value=CHANNEL_ID)
        ),
        patch.object(discord_notifier, "_ensure_task_thread", AsyncMock(return_value=None)),
    ):
        await discord_notifier._post_task_stream("guild-1", "t-1", "line")

    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_flush_task_stream_drains_and_removes_buffer():
    """flush_task_stream posts the pending lines and drops the buffer entry."""
    buf = discord_notifier._StreamBuffer(guild_id="guild-1")
    buf.lines = [discord_notifier._StreamEntry(line="a"), discord_notifier._StreamEntry(line="b")]
    buf.size = 4
    discord_notifier._stream_buffers["t-1"] = buf

    with patch.object(discord_notifier, "_post_task_stream", AsyncMock()) as post_mock:
        await discord_notifier.flush_task_stream("t-1")

    post_mock.assert_awaited_once_with("guild-1", "t-1", "a\nb")
    assert "t-1" not in discord_notifier._stream_buffers


@pytest.mark.asyncio
async def test_flush_task_stream_noop_when_no_buffer():
    """flush_task_stream is a silent no-op for a task with no buffered stream."""
    with patch.object(discord_notifier, "_post_task_stream", AsyncMock()) as post_mock:
        await discord_notifier.flush_task_stream("t-unknown")

    post_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Turn-summary grouping (issue #770)
# ---------------------------------------------------------------------------


def _entry(line, tool_type=None, name=None):
    detail = {"toolType": tool_type} if tool_type else None
    if detail and name:
        detail["name"] = name
    return discord_notifier._StreamEntry(line=line, detail=detail)


def test_format_stream_entries_groups_consecutive_tool_lines_into_a_turn():
    """Consecutive tool_use/tool_result entries collapse into one compact turn line."""
    entries = [
        _entry("▶ edit: foo.py", "tool_use", "Edit"),
        _entry("  → ok", "tool_result"),
        _entry("▶ bash: pytest", "tool_use", "Bash"),
        _entry("  → 3 passed", "tool_result"),
        _entry("▶ edit: bar.py", "tool_use", "Edit"),
        _entry("  → ok", "tool_result"),
    ]

    content, turn_count = discord_notifier._format_stream_entries(entries, 0)

    assert content == "▶ Turn 1: edit × 2, bash × 1"
    assert turn_count == 1


def test_format_stream_entries_passes_through_non_tool_lines():
    """Assistant text/thinking/result lines are posted as-is, not grouped."""
    entries = [_entry("some assistant text"), _entry("✓ Done in 3 turns")]

    content, turn_count = discord_notifier._format_stream_entries(entries, 0)

    assert content == "some assistant text\n✓ Done in 3 turns"
    assert turn_count == 0


def test_format_stream_entries_numbers_turns_across_flushes():
    """Turn numbers keep incrementing when starting from a prior batch's count."""
    first_batch = [
        _entry("▶ edit: foo.py", "tool_use", "Edit"),
        _entry("  → ok", "tool_result"),
        _entry("▶ bash: pytest", "tool_use", "Bash"),
        _entry("  → 3 passed", "tool_result"),
    ]
    _, turn_count = discord_notifier._format_stream_entries(first_batch, 0)

    second_batch = [
        _entry("▶ edit: foo.py", "tool_use", "Edit"),
        _entry("  → ok", "tool_result"),
        _entry("▶ bash: pytest", "tool_use", "Bash"),
        _entry("  → 3 passed", "tool_result"),
    ]
    content, turn_count = discord_notifier._format_stream_entries(second_batch, turn_count)

    assert content == "▶ Turn 2: edit × 1, bash × 1"
    assert turn_count == 2


def test_format_stream_entries_single_tool_call_shows_actual_tool_name():
    """A single tool call passes through its raw line instead of 'bash × 1' (issue #886)."""
    entries = [
        _entry("▶ bash: pytest -k test_foo", "tool_use", "Bash"),
        _entry("  → 3 passed", "tool_result"),
    ]

    content, turn_count = discord_notifier._format_stream_entries(entries, 0)

    assert content == "▶ bash: pytest -k test_foo\n||  → 3 passed||"
    assert turn_count == 0


def test_format_stream_entries_wraps_tool_result_output_in_spoiler_tags():
    """Tool output is spoiler-wrapped (issue #1101) so it's collapsed by default;
    the tool_use line naming what ran stays plainly visible."""
    entries = [
        _entry("▶ edit: notes.md", "tool_use", "Edit"),
        _entry("  → line one\n  → line two", "tool_result"),
    ]

    content, turn_count = discord_notifier._format_stream_entries(entries, 0)

    assert content == "▶ edit: notes.md\n||  → line one\n  → line two||"
    assert turn_count == 0


def test_format_stream_entries_single_tool_call_with_no_result():
    """A lone tool_use with no trailing tool_result still passes through raw.

    This is an expected production scenario, not just a defensive edge case: a
    batch can be flushed mid-turn (e.g. on a timer) before the matching
    tool_result has streamed in yet.
    """
    entries = [_entry("▶ write_file: notes.md", "tool_use", "Write")]

    content, turn_count = discord_notifier._format_stream_entries(entries, 0)

    assert content == "▶ write_file: notes.md"
    assert turn_count == 0


def test_format_stream_entries_zero_tool_calls_passes_through():
    """A run of only tool_result entries (no tool_use) also passes through, spoiler-wrapped."""
    entries = [_entry("  → stray result", "tool_result")]

    content, turn_count = discord_notifier._format_stream_entries(entries, 0)

    assert content == "||  → stray result||"
    assert turn_count == 0


@pytest.mark.asyncio
async def test_notify_task_stream_carries_detail_into_buffer(monkeypatch):
    """notify_task_stream stores the passed-through detail on the buffered entry."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_STREAM_TASKS", "1")

    with patch.object(discord_notifier, "_delayed_flush", AsyncMock()):
        await discord_notifier.notify_task_stream(
            "guild-1", "t-1", "▶ bash: ls", detail={"toolType": "tool_use", "name": "Bash"}
        )

        buf = discord_notifier._stream_buffers["t-1"]
        assert buf.lines == [
            discord_notifier._StreamEntry(
                line="▶ bash: ls", detail={"toolType": "tool_use", "name": "Bash"}
            )
        ]
        await buf.flush_task


@pytest.mark.asyncio
async def test_ensure_task_thread_reuses_existing():
    """_ensure_task_thread returns a persisted thread without hitting the Discord API."""
    with (
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(return_value=THREAD_ID)),
        patch.object(discord_notifier, "_create_thread_in_channel", AsyncMock()) as create_mock,
    ):
        result = await discord_notifier._ensure_task_thread("t-1", CHANNEL_ID)

    assert result == THREAD_ID
    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_task_thread_creates_and_names_from_description():
    """_ensure_task_thread creates a thread named from the task description when absent."""
    with (
        patch.object(discord_notifier, "_lookup_thread", AsyncMock(side_effect=[None, THREAD_ID])),
        patch.object(
            discord_notifier, "_lookup_task_description", AsyncMock(return_value="Fix the bug")
        ),
        patch.object(
            discord_notifier, "_create_thread_in_channel", AsyncMock(return_value=THREAD_ID)
        ) as create_mock,
        patch.object(discord_notifier, "_save_thread", AsyncMock()) as save_mock,
    ):
        result = await discord_notifier._ensure_task_thread("t-1", CHANNEL_ID)

    assert result == THREAD_ID
    create_mock.assert_awaited_once()
    thread_name = create_mock.call_args[0][1]
    assert "t-1" in thread_name
    assert "Fix the bug" in thread_name
    save_mock.assert_awaited_once_with(discord_notifier._SUBJECT_TASK_STREAM, "t-1", THREAD_ID)


def test_chunk_content_hard_splits_without_newlines():
    """A newline-free string is hard-split into <=size pieces that reconstruct exactly."""
    text = "x" * 5000
    chunks = discord_notifier._chunk_content(text, 2000)

    assert [len(c) for c in chunks] == [2000, 2000, 1000]
    assert "".join(chunks) == text


def test_chunk_content_prefers_newline_boundaries():
    """_chunk_content breaks on the last newline within the window when possible."""
    chunks = discord_notifier._chunk_content("aaaa\nbbbb\ncccc", 7)

    assert chunks == ["aaaa", "bbbb", "cccc"]
    assert all(len(c) <= 7 for c in chunks)


# ---------------------------------------------------------------------------
# Phase 7: CI check debounce/combine (issue #843)
# ---------------------------------------------------------------------------


def test_pr_debounce_seconds_defaults_to_15(monkeypatch):
    monkeypatch.delenv("DISCORD_PR_DEBOUNCE_SECONDS", raising=False)
    assert discord_notifier._pr_debounce_seconds() == 15.0


def test_pr_debounce_seconds_honours_env_override(monkeypatch):
    monkeypatch.setenv("DISCORD_PR_DEBOUNCE_SECONDS", "5")
    assert discord_notifier._pr_debounce_seconds() == 5.0


def test_format_ci_summary_all_passed():
    entries = [
        discord_notifier._CICheckEntry(check_name="lint", conclusion="success"),
        discord_notifier._CICheckEntry(check_name="test", conclusion="success"),
        discord_notifier._CICheckEntry(check_name="build", conclusion="success"),
    ]
    summary = discord_notifier._format_ci_summary(entries)
    assert summary == "✅ 3 checks passed: lint, test, build"


def test_format_ci_summary_mixed_pass_and_fail():
    entries = [
        discord_notifier._CICheckEntry(check_name="lint", conclusion="success"),
        discord_notifier._CICheckEntry(check_name="build", conclusion="failure"),
    ]
    summary = discord_notifier._format_ci_summary(entries)
    assert "✅ 1 check passed: lint" in summary
    assert "❌ 1 check failed: build" in summary


def test_format_ci_summary_singular_wording():
    entries = [discord_notifier._CICheckEntry(check_name="lint", conclusion="success")]
    assert discord_notifier._format_ci_summary(entries) == "✅ 1 check passed: lint"


@pytest.mark.asyncio
async def test_notify_ci_check_noop_when_no_bot_token(monkeypatch):
    """notify_ci_check must not buffer anything when the bot token is unset."""
    await discord_notifier.notify_ci_check("lint", "success", pr_label="org/repo#1")

    assert discord_notifier._ci_batches == {}


@pytest.mark.asyncio
async def test_notify_ci_check_buffers_without_posting_immediately(monkeypatch):
    """A single CI check is buffered and a delayed flush armed — no immediate POST."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_delayed_ci_flush", AsyncMock()),
    ):
        await discord_notifier.notify_ci_check(
            "lint", "success", pr_label="org/repo#1", issue_repo="org/repo", issue_number=1
        )

    mock_client.post.assert_not_called()
    batch = discord_notifier._ci_batches["org/repo#1"]
    assert batch.entries == [
        discord_notifier._CICheckEntry(check_name="lint", conclusion="success")
    ]


@pytest.mark.asyncio
async def test_notify_ci_check_combines_multiple_checks_into_one_message(monkeypatch):
    """Multiple rapid check completions for the same PR flush as one combined message."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        for name in ("lint", "test", "build"):
            await discord_notifier.notify_ci_check(
                name,
                "success",
                pr_label="org/repo#1",
                issue_repo="org/repo",
                issue_number=1,
            )
        # Flush directly instead of waiting out the real debounce window.
        await discord_notifier._flush_ci_batch("org/repo#1")

    mock_client.post.assert_called_once()
    body = mock_client.post.call_args[1]["json"]
    assert body["embeds"][0]["description"] == "✅ 3 checks passed: lint, test, build"
    assert "org/repo#1" not in discord_notifier._ci_batches


@pytest.mark.asyncio
async def test_flush_ci_batch_uses_ci_pass_when_all_succeed(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_ci_check(
            "lint", "success", pr_label="org/repo#1", issue_repo="org/repo", issue_number=1
        )
        await discord_notifier._flush_ci_batch("org/repo#1")

    body = mock_client.post.call_args[1]["json"]
    assert body["embeds"][0]["color"] == discord_notifier._COLOURS["ci-pass"]


@pytest.mark.asyncio
async def test_flush_ci_batch_uses_ci_fail_when_any_check_fails(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_ci_check(
            "lint", "success", pr_label="org/repo#1", issue_repo="org/repo", issue_number=1
        )
        await discord_notifier.notify_ci_check(
            "build", "failure", pr_label="org/repo#1", issue_repo="org/repo", issue_number=1
        )
        await discord_notifier._flush_ci_batch("org/repo#1")

    body = mock_client.post.call_args[1]["json"]
    assert body["embeds"][0]["color"] == discord_notifier._COLOURS["ci-fail"]


@pytest.mark.asyncio
async def test_flush_ci_batch_posts_with_suppress_notifications_flag(monkeypatch):
    """Combined CI summaries must always carry SUPPRESS_NOTIFICATIONS (flags=4096)."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_ci_check(
            "build", "failure", pr_label="org/repo#1", issue_repo="org/repo", issue_number=1
        )
        await discord_notifier._flush_ci_batch("org/repo#1")

    body = mock_client.post.call_args[1]["json"]
    assert body["flags"] == 4096 == discord_notifier._SILENT_FLAG


@pytest.mark.asyncio
async def test_flush_ci_batch_noop_when_batch_missing():
    """Flushing an already-flushed / unknown key is a silent no-op."""
    with patch.object(discord_notifier, "notify_event", AsyncMock()) as notify_mock:
        await discord_notifier._flush_ci_batch("unknown-key")

    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_notify_event_non_ci_has_no_suppress_flag(monkeypatch):
    """Non-CI events (e.g. pr-merged) must post without the silent flag."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("DISCORD_CHANNEL_ID", CHANNEL_ID)
    mock_client = _make_mock_client()

    with (
        patch.object(discord_notifier, "_get_client", return_value=mock_client),
        patch.object(discord_notifier, "_ensure_thread", AsyncMock(return_value=THREAD_ID)),
    ):
        await discord_notifier.notify_event(
            "pr-merged",
            title="PR merged",
            description="Merged!",
            issue_repo="org/repo",
            issue_number=1,
        )

    body = mock_client.post.call_args[1]["json"]
    assert "flags" not in body


@pytest.mark.asyncio
async def test_post_to_thread_omits_flags_key_when_not_given(monkeypatch):
    """_post_to_thread must not add a flags key to the payload when flags=None."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier._post_to_thread(THREAD_ID, "pr-opened", "t", "d")

    body = mock_client.post.call_args[1]["json"]
    assert "flags" not in body


@pytest.mark.asyncio
async def test_post_to_thread_includes_flags_when_given(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", BOT_TOKEN)
    mock_client = _make_mock_client()

    with patch.object(discord_notifier, "_get_client", return_value=mock_client):
        await discord_notifier._post_to_thread(THREAD_ID, "ci-pass", "t", "d", flags=4096)

    body = mock_client.post.call_args[1]["json"]
    assert body["flags"] == 4096
