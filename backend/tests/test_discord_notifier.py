"""Unit tests for discord_notifier.

No real HTTP requests are made — the httpx call is mocked throughout.
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest
import respx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import discord_notifier

WEBHOOK_URL = "https://discord.com/api/webhooks/test/token"


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Start each test with no webhook URL set."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)


# ---------------------------------------------------------------------------
# No-op when env var unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_no_op_when_env_unset():
    """notify() must not make any HTTP call when DISCORD_WEBHOOK_URL is unset."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        await discord_notifier.notify("task-complete", "Done", "All good")
        assert not route.called


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_posts_correct_embed(monkeypatch):
    """notify() builds the right embed payload and POSTs it to the webhook."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)

    with respx.mock() as mock:
        route = mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        await discord_notifier.notify(
            "pr-opened", "PR #42 opened", "New pull request", url="https://example.com/pr/42"
        )

    assert route.called
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["embeds"][0]["title"] == "PR #42 opened"
    assert body["embeds"][0]["description"] == "New pull request"
    assert body["embeds"][0]["url"] == "https://example.com/pr/42"
    assert body["embeds"][0]["color"] == 0x3498DB  # blue


@pytest.mark.asyncio
async def test_notify_no_url_field_when_omitted(monkeypatch):
    """url field must be absent from the embed when url=None."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)

    with respx.mock() as mock:
        route = mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        await discord_notifier.notify("worker-online", "Worker connected", "w-abc is online")

    import json

    body = json.loads(route.calls.last.request.content)
    assert "url" not in body["embeds"][0]


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

    import json

    with respx.mock() as mock:
        route = mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        await discord_notifier.notify(event_type, "title", "desc")

    body = json.loads(route.calls.last.request.content)
    assert body["embeds"][0]["color"] == expected_color


@pytest.mark.asyncio
async def test_custom_color_overrides_event_type(monkeypatch):
    """Explicit color= kwarg takes precedence over the event-type colour map."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)

    import json

    with respx.mock() as mock:
        route = mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
        await discord_notifier.notify("task-complete", "t", "d", color=0xABCDEF)

    body = json.loads(route.calls.last.request.content)
    assert body["embeds"][0]["color"] == 0xABCDEF


# ---------------------------------------------------------------------------
# HTTP failure must not raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_does_not_raise(monkeypatch):
    """An HTTP error must be swallowed and logged, never propagated."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)

    with respx.mock() as mock:
        mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
        # Should not raise despite 500 response
        await discord_notifier.notify("task-failed", "Failed", "oh no")


@pytest.mark.asyncio
async def test_network_error_does_not_raise(monkeypatch):
    """A network-level exception (connection refused etc.) must be swallowed."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK_URL)

    with respx.mock() as mock:
        mock.post(WEBHOOK_URL).mock(side_effect=httpx.ConnectError("refused"))
        await discord_notifier.notify("task-failed", "Failed", "network down")
