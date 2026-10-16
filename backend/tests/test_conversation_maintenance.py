"""Tests for the periodic conversation-health sweep (issues #1167, #1299).

``sweep_conversations`` is the Foreman-side periodic mechanism that ages
conversations along their lifecycle (active -> archived -> closed). It is
driven entirely by the Foreman's own periodic-check cycle — it never reacts
to Discord events.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
import foreman.conversation_maintenance as conversation_maintenance
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from foreman.conversation_service import get_or_create_active_conversation
from helpers import create_db, insert_guild, truncate_all
from models import Conversation


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # Keep the sweep from actually archiving/closing anything unless a test
    # explicitly lowers these below zero — real defaults are days-long.
    monkeypatch.setattr(conversation_maintenance, "CONVERSATION_AUTO_ARCHIVE_AFTER_SECONDS", 3 * 24 * 3600.0)
    monkeypatch.setattr(conversation_maintenance, "CONVERSATION_AUTO_CLOSE_AFTER_SECONDS", 14 * 24 * 3600.0)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


async def _guild_pk(guild_id: str) -> int:
    async with database_module.AsyncSessionLocal() as db:
        pk = await get_guild_pk(db, guild_id)
    assert pk is not None
    return pk


async def _backdate(conversation_id: int, updated_at: datetime, status: str | None = None) -> None:
    async with database_module.AsyncSessionLocal() as db:
        conversation = await db.get(Conversation, conversation_id)
        conversation.updated_at = updated_at
        if status:
            conversation.status = status
        db.add(conversation)
        await db.commit()


async def _new_conversation(guild_id: str) -> int:
    guild_pk = await _guild_pk(guild_id)
    async with database_module.AsyncSessionLocal() as db:
        conversation, _ = await get_or_create_active_conversation(db, guild_pk, "user-1")
    return conversation.id


async def _status(conversation_id: int) -> str:
    async with database_module.AsyncSessionLocal() as db:
        return (await db.get(Conversation, conversation_id)).status


ZERO = {"archived": 0, "closed": 0}


class TestSweepConversations:
    async def test_leaves_fresh_active_conversation_untouched(self, db_session):
        insert_guild(db_session, "g-sweep-fresh")
        conv_id = await _new_conversation("g-sweep-fresh")

        summary = await conversation_maintenance.sweep_conversations("g-sweep-fresh")

        assert summary == ZERO
        assert await _status(conv_id) == "active"

    async def test_archives_stale_active_conversation(self, db_session):
        insert_guild(db_session, "g-sweep-archive")
        conv_id = await _new_conversation("g-sweep-archive")
        await _backdate(conv_id, datetime.now(UTC) - timedelta(days=4))

        with patch("discord_notifier.is_configured", return_value=False):
            summary = await conversation_maintenance.sweep_conversations("g-sweep-archive")

        assert summary == {"archived": 1, "closed": 0}
        assert await _status(conv_id) == "archived"

    async def test_closes_stale_archived_conversation(self, db_session):
        insert_guild(db_session, "g-sweep-close")
        conv_id = await _new_conversation("g-sweep-close")
        await _backdate(conv_id, datetime.now(UTC) - timedelta(days=15), status="archived")

        with patch("discord_notifier.is_configured", return_value=False):
            summary = await conversation_maintenance.sweep_conversations("g-sweep-close")

        assert summary == {"archived": 0, "closed": 1}
        assert await _status(conv_id) == "closed"

    async def test_archive_mirrors_to_discord_and_broadcasts(self, db_session):
        insert_guild(db_session, "g-sweep-mirror")
        conv_id = await _new_conversation("g-sweep-mirror")
        async with database_module.AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, conv_id)
            conversation.discord_thread_id = "discord-sweep-1"
            db.add(conversation)
            await db.commit()
        await _backdate(conv_id, datetime.now(UTC) - timedelta(days=4))

        with (
            patch("discord_notifier.is_configured", return_value=False),
            patch(
                "discord.thread_mirror.on_thread_updated", new_callable=AsyncMock
            ) as mock_mirror,
            patch(
                "foreman.conversation_service.broadcast", new_callable=AsyncMock
            ) as mock_broadcast,
        ):
            await conversation_maintenance.sweep_conversations("g-sweep-mirror")

        mock_mirror.assert_awaited_once_with("discord-sweep-1", status="archived")
        assert mock_broadcast.await_args.args[1]["type"] == "conversation-updated"

    async def test_unknown_guild_returns_zeroed_summary(self, db_session):
        summary = await conversation_maintenance.sweep_conversations("g-does-not-exist")
        assert summary == ZERO

    async def test_notifies_discord_only_when_something_changed(self, db_session):
        insert_guild(db_session, "g-sweep-notify")
        conv_id = await _new_conversation("g-sweep-notify")
        await _backdate(conv_id, datetime.now(UTC) - timedelta(days=4))

        with (
            patch("discord_notifier.is_configured", return_value=True),
            patch("discord_notifier.notify", new_callable=AsyncMock) as mock_notify,
            patch("discord_notifier._bot_request", new_callable=AsyncMock),
        ):
            await conversation_maintenance.sweep_conversations("g-sweep-notify")

        mock_notify.assert_awaited_once()
        assert mock_notify.call_args.kwargs["ps_guild_slug"] == "g-sweep-notify"

    async def test_quiet_sweep_does_not_notify_discord(self, db_session):
        insert_guild(db_session, "g-sweep-quiet")
        await _new_conversation("g-sweep-quiet")

        with (
            patch("discord_notifier.is_configured", return_value=True),
            patch("discord_notifier.notify", new_callable=AsyncMock) as mock_notify,
        ):
            await conversation_maintenance.sweep_conversations("g-sweep-quiet")

        mock_notify.assert_not_awaited()

    async def test_sweep_never_raises_on_db_error(self, db_session, monkeypatch):
        async def _boom(*args, **kwargs):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(conversation_maintenance, "_sweep_conversations_once", _boom)

        summary = await conversation_maintenance.sweep_conversations("g-anything")

        assert summary == ZERO
