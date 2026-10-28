"""Tests for routes/conversations.py — Conversation rename/close (#1278).

Covered directly rather than through the HTTP layer — the auth/HTTP wiring
is exercised elsewhere; this focuses on the route handlers'
Conversation<->Discord mirroring side effect.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
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
from foreman.conversation_service import close_conversation, get_or_create_active_conversation
from helpers import create_db, insert_conversation, insert_guild, truncate_all
from models import Conversation, Message
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
            conv, _ = await get_or_create_active_conversation(db, guild_pk, "user-1")

        from discord.thread_mirror import _stamp_discord_thread_id

        await _stamp_discord_thread_id(conv.id, "discord-thread-route-rn")

        with patch(
            "discord.thread_mirror.rename_conversation_thread", new_callable=AsyncMock
        ) as mock_rename:
            async with database_module.AsyncSessionLocal() as db:
                out = await rename_conversation_route(
                    "g-conv-rename",
                    conv.id,
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
    async def test_closes_conversation(self, db_session):
        insert_guild(db_session, "g-conv-close")
        guild_pk = await _guild_pk("g-conv-close")

        async with database_module.AsyncSessionLocal() as db:
            conv, _ = await get_or_create_active_conversation(db, guild_pk, "user-1")

        from discord.thread_mirror import _stamp_discord_thread_id

        await _stamp_discord_thread_id(conv.id, "discord-thread-route-cc")

        with patch(
            "discord.thread_mirror.archive_conversation_thread_by_id", new_callable=AsyncMock
        ) as mock_archive:
            async with database_module.AsyncSessionLocal() as db:
                out = await close_conversation_route("g-conv-close", conv.id, "user-1", db)

        assert out.status == "closed"
        mock_archive.assert_called_once_with("discord-thread-route-cc")


class TestGetConversationRoute:
    async def test_returns_conversation_out(self, db_session):
        insert_guild(db_session, "g-conv-get")
        guild_pk = await _guild_pk("g-conv-get")

        async with database_module.AsyncSessionLocal() as db:
            conv, _ = await get_or_create_active_conversation(db, guild_pk, "user-1")

        async with database_module.AsyncSessionLocal() as db:
            out = await get_conversation("g-conv-get", conv.id, "user-1", db)

        assert out.id == conv.id
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


class TestSearchConversationsRoute:
    @pytest.mark.parametrize(
        ("query", "status", "matches"),
        [
            (" PIPELINE ", None, True),
            ("needle", None, True),
            ("100%_", None, True),
            ("needle", "archived", True),
            ("needle", "active", False),
            ("missing", None, False),
        ],
    )
    async def test_searches_names_and_messages_within_guild(
        self, db_session, query, status, matches
    ):
        insert_guild(db_session, "g-search")
        insert_guild(db_session, "g-other")
        guild_pk = await _guild_pk("g-search")
        now = datetime.now(UTC)
        async with database_module.AsyncSessionLocal() as db:
            conversation = Conversation(
                guild_id=guild_pk,
                name="Deploy pipeline",
                status="archived",
                created_at=now,
                updated_at=now,
            )
            other = Conversation(
                guild_id=await _guild_pk("g-other"),
                name="Deploy pipeline",
                created_at=now,
                updated_at=now,
            )
            nonmatch = Conversation(
                guild_id=guild_pk,
                name="100xx",
                created_at=now,
                updated_at=now,
            )
            db.add_all([conversation, other, nonmatch])
            await db.flush()
            for owner in (conversation, other):
                for _ in range(2):
                    db.add(
                        Message(
                            guild_id=owner.guild_id,
                            conversation_id=owner.id,
                            content="A needle at 100%_ completion",
                            message_type="chat",
                            created_at=now,
                        )
                    )
            await db.commit()
            out = await list_conversations("g-search", status, "user-1", db, q=query)

        assert [c.id for c in out] == ([conversation.id] if matches else [])

    async def test_searches_before_applying_result_limit(self, db_session):
        insert_guild(db_session, "g-search-old")
        guild_pk = await _guild_pk("g-search-old")
        now = datetime.now(UTC)
        async with database_module.AsyncSessionLocal() as db:
            older = Conversation(
                guild_id=guild_pk,
                name="Old needle",
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
            )
            db.add(older)
            db.add_all(
                [
                    Conversation(guild_id=guild_pk, name="Recent", created_at=now, updated_at=now)
                    for _ in range(200)
                ]
            )
            await db.commit()
            out = await list_conversations("g-search-old", None, "user-1", db)
            assert older.id not in [c.id for c in out]
            out = await list_conversations("g-search-old", None, "user-1", db, q="needle")
            assert [c.id for c in out] == [older.id]


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
