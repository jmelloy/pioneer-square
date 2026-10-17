"""Tests for discord/thread_mirror.py — Foreman thread mirroring to Discord (#1168)."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from discord.thread_mirror import (
    _stamp_discord_thread_id,
    archive_conversation_thread_by_id,
    on_conversation_created,
    on_thread_updated,
    relay_discord_thread_event,
    rename_conversation_thread,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from foreman.conversation_service import get_or_create_active_conversation
from helpers import create_db, insert_guild, truncate_all
from models import Conversation


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
def mock_stamp():
    with patch("discord.thread_mirror._stamp_discord_thread_id", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_bot_request():
    with patch("discord_notifier._bot_request", new_callable=AsyncMock) as m:
        yield m


class TestOnConversationCreated:
    """Test on_conversation_created creates Discord threads in response to Foreman."""

    async def test_creates_discord_thread_on_conversation_created(
        self, mock_bot_token, mock_channel, mock_create_thread, mock_stamp
    ):
        result = await on_conversation_created(
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
        mock_stamp.assert_called_once_with(1, "discord-thread-999")

    async def test_noop_when_not_configured(self):
        with patch("discord_notifier.is_configured", return_value=False):
            result = await on_conversation_created(
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
            result = await on_conversation_created(
                conversation_id=1,
                guild_slug="g",
            )
            assert result is None


class TestOnThreadUpdated:
    """Test on_thread_updated mirrors Foreman status to Discord.

    Issue #1288: on_thread_updated is keyed by the Discord thread id the
    caller already has in hand (``Conversation.discord_thread_id``) — it does no
    lookup of its own, so these tests pass it directly instead of mocking a
    lookup helper.
    """

    async def test_archives_discord_thread_when_foreman_archives(
        self, mock_bot_token, mock_bot_request
    ):
        await on_thread_updated("discord-thread-999", status="archived")
        mock_bot_request.assert_called_once_with(
            "patch",
            "/channels/discord-thread-999",
            {"archived": True},
        )

    async def test_unarchives_discord_thread_when_foreman_reactivates(
        self, mock_bot_token, mock_bot_request
    ):
        await on_thread_updated("discord-thread-999", status="active")
        mock_bot_request.assert_called_once_with(
            "patch",
            "/channels/discord-thread-999",
            {"archived": False},
        )

    async def test_noop_when_no_discord_thread(self, mock_bot_token, mock_bot_request):
        # Should not raise
        await on_thread_updated(None, status="archived")
        mock_bot_request.assert_not_called()


class TestRelayDiscordThreadEvent:
    """Test relay_discord_thread_event does NOT change Foreman state."""

    async def test_does_not_modify_conversation_status(self):
        """Relay logs the event but leaves Conversation.status unchanged.

        We mock the DB layer and verify that relay_discord_thread_event never
        writes back to the Conversation row — it only reads and logs.
        """
        mock_thread = AsyncMock()
        mock_thread.id = 7
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
        # Conversation status was never reassigned
        assert mock_thread.status == "active"


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


async def _guild_pk(guild_id: str) -> int:
    async with database_module.AsyncSessionLocal() as db:
        pk = await get_guild_pk(db, guild_id)
    assert pk is not None
    return pk


class TestStampDiscordThreadIdMirrorsConversation:
    """Stamping writes ``discord_thread_id`` straight onto the Conversation,
    which ``discord_notifier._lookup_foreman_thread_for_user`` reads."""

    async def test_stamp_sets_discord_thread_id_on_conversation(self, db_session):
        insert_guild(db_session, "g-stamp-mirror")
        guild_pk = await _guild_pk("g-stamp-mirror")

        async with database_module.AsyncSessionLocal() as db:
            created, _ = await get_or_create_active_conversation(db, guild_pk, "user-1")

        await _stamp_discord_thread_id(created.id, "discord-thread-mirrored")

        async with database_module.AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, created.id)
        assert conversation.discord_thread_id == "discord-thread-mirrored"


class TestRenameConversationThread:
    """Issue #1278: renaming/archiving a Conversation's Discord thread directly
    from Conversation.discord_thread_id, ."""

    async def test_renames_via_discord_thread_id(self, mock_bot_token, mock_bot_request):
        await rename_conversation_thread("discord-thread-999", "New Name")
        mock_bot_request.assert_called_once_with(
            "patch", "/channels/discord-thread-999", {"name": "New Name"}
        )

    async def test_rename_truncates_to_100_chars(self, mock_bot_token, mock_bot_request):
        long_name = "x" * 150
        await rename_conversation_thread("discord-thread-999", long_name)
        args = mock_bot_request.call_args[0]
        assert len(args[2]["name"]) == 100

    async def test_rename_noop_when_not_configured(self, mock_bot_request):
        with patch("discord_notifier.is_configured", return_value=False):
            await rename_conversation_thread("discord-thread-999", "New Name")
        mock_bot_request.assert_not_called()

    async def test_archives_via_discord_thread_id(self, mock_bot_token, mock_bot_request):
        await archive_conversation_thread_by_id("discord-thread-999")
        mock_bot_request.assert_called_once_with(
            "patch", "/channels/discord-thread-999", {"archived": True}
        )

    async def test_archive_noop_when_not_configured(self, mock_bot_request):
        with patch("discord_notifier.is_configured", return_value=False):
            await archive_conversation_thread_by_id("discord-thread-999")
        mock_bot_request.assert_not_called()
