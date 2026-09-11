"""Tests for routes/conversations.py — Conversation rename/close (#1278).

Covered directly rather than through the HTTP layer — the auth/HTTP wiring
is exercised elsewhere (see ``test_threads_routes_conversation_sync.py`` for
the equivalent Thread-scoped precedent); this focuses on the route handlers'
Conversation<->Discord mirroring side effect.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from foreman import triggers
from foreman.conversation_service import close_conversation
from foreman.thread_service import get_or_create_active_thread
from helpers import create_db, insert_conversation, insert_guild, truncate_all
from models import Conversation, Thread
from routes.conversations import (
    ConversationMessageCreate,
    ConversationRename,
    close_conversation_route,
    get_conversation,
    list_conversation_messages,
    list_conversations,
    post_conversation_message,
    rename_conversation_route,
)


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


class TestRenameConversationRoute:
    async def test_renames_conversation_and_discord_thread(self, db_session):
        insert_guild(db_session, "g-conv-rename")
        guild_pk = await _guild_pk("g-conv-rename")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

        from discord.thread_mirror import _stamp_discord_thread_id

        await _stamp_discord_thread_id(thread.id, "discord-thread-route-rn")

        with patch(
            "discord.thread_mirror.rename_conversation_thread", new_callable=AsyncMock
        ) as mock_rename:
            async with database_module.AsyncSessionLocal() as db:
                out = await rename_conversation_route(
                    "g-conv-rename",
                    thread.conversation_id,
                    ConversationRename(name="Renamed"),
                    "user-1",
                    db,
                )

        assert out.name == "Renamed"
        mock_rename.assert_called_once_with("discord-thread-route-rn", "Renamed")

    async def test_404_for_unknown_conversation(self, db_session):
        insert_guild(db_session, "g-conv-missing")

        async with database_module.AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await rename_conversation_route(
                    "g-conv-missing", 999999, ConversationRename(name="x"), "user-1", db
                )
        assert exc_info.value.status_code == 404


class TestCloseConversationRoute:
    async def test_closes_conversation_and_thread(self, db_session):
        insert_guild(db_session, "g-conv-close")
        guild_pk = await _guild_pk("g-conv-close")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

        from discord.thread_mirror import _stamp_discord_thread_id

        await _stamp_discord_thread_id(thread.id, "discord-thread-route-cc")

        with patch(
            "discord.thread_mirror.archive_conversation_thread_by_id", new_callable=AsyncMock
        ) as mock_archive:
            async with database_module.AsyncSessionLocal() as db:
                out = await close_conversation_route(
                    "g-conv-close", thread.conversation_id, "user-1", db
                )

        assert out.status == "closed"
        mock_archive.assert_called_once_with("discord-thread-route-cc")

        async with database_module.AsyncSessionLocal() as db:
            refreshed_thread = await db.get(Thread, thread.id)
        assert refreshed_thread.status == "closed"


class TestGetConversationRoute:
    async def test_returns_conversation_out(self, db_session):
        insert_guild(db_session, "g-conv-get")
        guild_pk = await _guild_pk("g-conv-get")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

        async with database_module.AsyncSessionLocal() as db:
            out = await get_conversation("g-conv-get", thread.conversation_id, "user-1", db)

        assert out.id == thread.conversation_id
        assert out.status == "active"


class TestListConversationsRoute:
    """GET /api/guilds/{guild_id}/conversations (#1298)."""

    async def test_lists_conversations_most_recently_updated_first(self, db_session):
        insert_guild(db_session, "g-conv-list")
        older_id = insert_conversation(db_session, "g-conv-list", user_id="user-1")
        newer_id = insert_conversation(db_session, "g-conv-list", user_id="user-2")

        async with database_module.AsyncSessionLocal() as db:
            out = await list_conversations("g-conv-list", None, "user-1", db)

        assert [c.id for c in out] == [newer_id, older_id]

    async def test_filters_by_status(self, db_session):
        insert_guild(db_session, "g-conv-list-status")
        conv_id = insert_conversation(db_session, "g-conv-list-status", user_id="user-1")

        async with database_module.AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, conv_id)
            await close_conversation(db, conversation)

        async with database_module.AsyncSessionLocal() as db:
            active_only = await list_conversations("g-conv-list-status", "active", "user-1", db)
            closed_only = await list_conversations("g-conv-list-status", "closed", "user-1", db)

        assert active_only == []
        assert [c.id for c in closed_only] == [conv_id]

    async def test_rejects_invalid_status(self, db_session):
        insert_guild(db_session, "g-conv-list-bad-status")

        async with database_module.AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await list_conversations("g-conv-list-bad-status", "bogus", "user-1", db)
        assert exc_info.value.status_code == 400

    async def test_404_for_unknown_guild(self, db_session):
        async with database_module.AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await list_conversations("g-does-not-exist", None, "user-1", db)
        assert exc_info.value.status_code == 404

    async def test_scoped_to_guild(self, db_session):
        insert_guild(db_session, "g-conv-list-a")
        insert_guild(db_session, "g-conv-list-b")
        insert_conversation(db_session, "g-conv-list-a", user_id="user-1")

        async with database_module.AsyncSessionLocal() as db:
            out = await list_conversations("g-conv-list-b", None, "user-1", db)

        assert out == []


class TestListConversationMessagesRoute:
    """GET /api/guilds/{guild_id}/conversations/{conversation_id}/messages (#1298)."""

    async def test_returns_messages_oldest_first(self, db_session):
        insert_guild(db_session, "g-conv-msgs")
        conv_id = insert_conversation(db_session, "g-conv-msgs", user_id="user-1")

        with patch.object(triggers, "trigger_foreman", new=AsyncMock()):
            async with database_module.AsyncSessionLocal() as db:
                await post_conversation_message(
                    "g-conv-msgs", conv_id, ConversationMessageCreate(content="first"), "user-1", db
                )
            async with database_module.AsyncSessionLocal() as db:
                await post_conversation_message(
                    "g-conv-msgs",
                    conv_id,
                    ConversationMessageCreate(content="second"),
                    "user-1",
                    db,
                )

        async with database_module.AsyncSessionLocal() as db:
            out = await list_conversation_messages("g-conv-msgs", conv_id, "user-1", db)

        assert [m["content"] for m in out] == ["first", "second"]

    async def test_404_for_unknown_conversation(self, db_session):
        insert_guild(db_session, "g-conv-msgs-missing")

        async with database_module.AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as exc_info:
                await list_conversation_messages("g-conv-msgs-missing", 999999, "user-1", db)
        assert exc_info.value.status_code == 404
