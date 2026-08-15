"""Tests for discord/thread_mirror.py — Foreman thread mirroring to Discord (#1168)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from discord.thread_mirror import (
    _get_discord_thread_id,
    _stamp_discord_thread_id,
    on_thread_created,
    on_thread_updated,
    relay_discord_thread_event,
)


@pytest.fixture
def mock_bot_token():
    with patch("discord_notifier.bot_token", return_value="fake-token"):
        yield


@pytest.fixture
def mock_channel():
    with patch(
        "discord_notifier._resolve_channel_for_guild",
        new_callable=AsyncMock,
        return_value="chan-123",
    ):
        yield


@pytest.fixture
def mock_create_thread():
    with patch(
        "discord_notifier._create_thread_in_channel",
        new_callable=AsyncMock,
        return_value="discord-thread-999",
    ) as m:
        yield m


@pytest.fixture
def mock_save_thread():
    with patch("discord_notifier._save_thread", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_stamp():
    with patch("discord.thread_mirror._stamp_discord_thread_id", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_bot_request():
    with patch("discord_notifier._bot_request", new_callable=AsyncMock) as m:
        yield m


class TestOnThreadCreated:
    """Test on_thread_created creates Discord threads in response to Foreman."""

    async def test_creates_discord_thread_on_foreman_thread_created(
        self, mock_bot_token, mock_channel, mock_create_thread, mock_save_thread, mock_stamp
    ):
        result = await on_thread_created(
            thread_id="th-abc123",
            conversation_id=1,
            guild_slug="my-guild",
            name="Fix the auth bug",
            user_id="user-1",
        )
        assert result == "discord-thread-999"
        mock_create_thread.assert_called_once()
        # Thread name should have the 💬 prefix
        call_args = mock_create_thread.call_args
        assert "💬" in call_args[0][1]
        mock_stamp.assert_called_once_with("th-abc123", "discord-thread-999")
        mock_save_thread.assert_called_once_with("conversation", "th-abc123", "discord-thread-999")

    async def test_noop_when_not_configured(self):
        with patch("discord_notifier.is_configured", return_value=False):
            result = await on_thread_created(
                thread_id="th-abc",
                conversation_id=1,
                guild_slug="g",
            )
            assert result is None

    async def test_noop_when_no_channel(self, mock_bot_token):
        with patch(
            "discord_notifier._resolve_channel_for_guild",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await on_thread_created(
                thread_id="th-abc",
                conversation_id=1,
                guild_slug="g",
            )
            assert result is None


class TestOnThreadUpdated:
    """Test on_thread_updated mirrors Foreman status to Discord."""

    async def test_archives_discord_thread_when_foreman_archives(
        self, mock_bot_token, mock_bot_request
    ):
        with patch(
            "discord.thread_mirror._get_discord_thread_id",
            new_callable=AsyncMock,
            return_value="discord-thread-999",
        ):
            await on_thread_updated(thread_id="th-abc", status="archived")
            mock_bot_request.assert_called_once_with(
                "patch",
                "/channels/discord-thread-999",
                {"archived": True},
            )

    async def test_unarchives_discord_thread_when_foreman_reactivates(
        self, mock_bot_token, mock_bot_request
    ):
        with patch(
            "discord.thread_mirror._get_discord_thread_id",
            new_callable=AsyncMock,
            return_value="discord-thread-999",
        ):
            await on_thread_updated(thread_id="th-abc", status="active")
            mock_bot_request.assert_called_once_with(
                "patch",
                "/channels/discord-thread-999",
                {"archived": False},
            )

    async def test_noop_when_no_discord_thread(self, mock_bot_token):
        with patch(
            "discord.thread_mirror._get_discord_thread_id",
            new_callable=AsyncMock,
            return_value=None,
        ):
            # Should not raise
            await on_thread_updated(thread_id="th-abc", status="archived")


class TestRelayDiscordThreadEvent:
    """Test relay_discord_thread_event does NOT change Foreman state."""

    async def test_does_not_modify_foreman_thread_status(self):
        """Relay logs the event but leaves Thread.status unchanged.

        We mock the DB layer and verify that relay_discord_thread_event never
        writes back to the Thread row — it only reads and logs.
        """
        mock_thread = AsyncMock()
        mock_thread.id = "th-relay-test"
        mock_thread.status = "active"
        mock_thread.discord_thread_id = "discord-999"

        mock_result = AsyncMock()
        mock_result.first.return_value = mock_thread

        mock_db = AsyncMock()
        mock_db.exec = AsyncMock(return_value=mock_result)
        mock_db.commit = AsyncMock()

        mock_session_cls = AsyncMock()
        mock_session_cls.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_cls.__aexit__ = AsyncMock(return_value=False)

        with patch("database.AsyncSessionLocal", return_value=mock_session_cls):
            # Relay an "archived" event from Discord
            await relay_discord_thread_event("discord-999", "archived")

        # Verify: no commit was called (no state mutation)
        mock_db.commit.assert_not_called()
        # Thread status was never reassigned
        assert mock_thread.status == "active"
