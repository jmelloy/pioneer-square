"""Tests for Foreman-owned thread lifecycle (issue #1167).

Per epic #1160's "Architectural correction" comment (and #1167's explicit
non-goals), a :class:`models.Thread` is a Foreman construct: created/reused
as a side effect of the Foreman handling a message, never something Discord
or the frontend originates. These tests cover ``thread_service.py``'s
get-or-create semantics, the ``ensure_conversation_thread`` entry point
``foreman.triggers.trigger_foreman`` calls, and ``Task.thread_id`` stamping.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from foreman.thread_service import (
    broadcast_thread_updated,
    ensure_conversation_thread,
    get_or_create_active_thread,
    get_or_create_conversation,
    get_thread_for_task,
    reactivate_conversation_thread,
    sync_conversation_after_thread_update,
)
from helpers import create_db, insert_guild, insert_task, truncate_all
from models import Conversation, Task, Thread


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


# ── get_or_create_conversation ───────────────────────────────────────────────


class TestGetOrCreateConversation:
    async def test_creates_new_conversation(self, db_session):
        insert_guild(db_session, "g-conv1")
        guild_pk = await _guild_pk("g-conv1")

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()

        assert conv.id is not None
        assert conv.guild_id == guild_pk
        assert conv.user_id == "user-1"

    async def test_reuses_existing_conversation_for_same_user(self, db_session):
        insert_guild(db_session, "g-conv2")
        guild_pk = await _guild_pk("g-conv2")

        async with database_module.AsyncSessionLocal() as db:
            conv1 = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()
            conv1_id = conv1.id

        async with database_module.AsyncSessionLocal() as db:
            conv2 = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()

        assert conv2.id == conv1_id

    async def test_different_users_get_different_conversations(self, db_session):
        insert_guild(db_session, "g-conv3")
        guild_pk = await _guild_pk("g-conv3")

        async with database_module.AsyncSessionLocal() as db:
            conv_a = await get_or_create_conversation(db, guild_pk, "user-a")
            await db.commit()
        async with database_module.AsyncSessionLocal() as db:
            conv_b = await get_or_create_conversation(db, guild_pk, "user-b")
            await db.commit()

        assert conv_a.id != conv_b.id


# ── get_or_create_active_thread ──────────────────────────────────────────────


class TestGetOrCreateActiveThread:
    async def test_creates_new_thread_for_first_message(self, db_session):
        insert_guild(db_session, "g-thread1")
        guild_pk = await _guild_pk("g-thread1")

        async with database_module.AsyncSessionLocal() as db:
            thread, created = await get_or_create_active_thread(
                db, guild_pk, "user-1", name_hint="hello there"
            )

        assert created is True
        assert thread.status == "active"
        assert thread.name == "hello there"
        assert thread.discord_thread_id is None

    async def test_reuses_active_thread_for_same_conversation(self, db_session):
        insert_guild(db_session, "g-thread2")
        guild_pk = await _guild_pk("g-thread2")

        async with database_module.AsyncSessionLocal() as db:
            thread1, created1 = await get_or_create_active_thread(db, guild_pk, "user-1")
        async with database_module.AsyncSessionLocal() as db:
            thread2, created2 = await get_or_create_active_thread(db, guild_pk, "user-1")

        assert created1 is True
        assert created2 is False
        assert thread1.id == thread2.id

    async def test_reuse_bumps_updated_at(self, db_session):
        insert_guild(db_session, "g-thread3")
        guild_pk = await _guild_pk("g-thread3")

        async with database_module.AsyncSessionLocal() as db:
            thread1, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            first_updated = thread1.updated_at

        async with database_module.AsyncSessionLocal() as db:
            thread2, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

        assert thread2.updated_at >= first_updated

    async def test_new_thread_created_when_previous_is_archived(self, db_session):
        insert_guild(db_session, "g-thread4")
        guild_pk = await _guild_pk("g-thread4")

        async with database_module.AsyncSessionLocal() as db:
            thread1, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            thread1.status = "archived"
            db.add(thread1)
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            thread2, created2 = await get_or_create_active_thread(db, guild_pk, "user-1")

        assert created2 is True
        assert thread2.id != thread1.id

    async def test_different_users_get_different_threads(self, db_session):
        insert_guild(db_session, "g-thread5")
        guild_pk = await _guild_pk("g-thread5")

        async with database_module.AsyncSessionLocal() as db:
            thread_a, _ = await get_or_create_active_thread(db, guild_pk, "user-a")
        async with database_module.AsyncSessionLocal() as db:
            thread_b, _ = await get_or_create_active_thread(db, guild_pk, "user-b")

        assert thread_a.id != thread_b.id


# ── get_thread_for_task ──────────────────────────────────────────────────────


class TestGetThreadForTask:
    async def test_returns_none_when_task_has_no_thread(self, db_session):
        insert_guild(db_session, "g-task1")
        insert_task(db_session, "g-task1", "t-notask1")

        async with database_module.AsyncSessionLocal() as db:
            task = await db.get(Task, "t-notask1")
            thread = await get_thread_for_task(db, task)

        assert thread is None

    async def test_returns_thread_when_task_has_thread(self, db_session):
        insert_guild(db_session, "g-task2")
        guild_pk = await _guild_pk("g-task2")
        insert_task(db_session, "g-task2", "t-hasthread")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            task = await db.get(Task, "t-hasthread")
            task.thread_id = thread.id
            db.add(task)
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            task = await db.get(Task, "t-hasthread")
            found = await get_thread_for_task(db, task)

        assert found is not None
        assert found.id == thread.id

    async def test_returns_none_when_thread_soft_deleted(self, db_session):
        insert_guild(db_session, "g-task3")
        guild_pk = await _guild_pk("g-task3")
        insert_task(db_session, "g-task3", "t-deletedthread")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            task = await db.get(Task, "t-deletedthread")
            task.thread_id = thread.id
            db.add(task)

            from datetime import UTC, datetime

            thread.deleted_at = datetime.now(UTC)
            db.add(thread)
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            task = await db.get(Task, "t-deletedthread")
            found = await get_thread_for_task(db, task)

        assert found is None


# ── ensure_conversation_thread (the ws_handlers entry point) ────────────────


class TestEnsureConversationThread:
    async def test_creates_thread_and_broadcasts_once(self, db_session):
        insert_guild(db_session, "g-ensure1")

        with patch("foreman.thread_service.broadcast", new_callable=AsyncMock) as mock_broadcast:
            thread = await ensure_conversation_thread("g-ensure1", "user-1", "hi there")

        assert thread is not None
        assert thread.status == "active"
        mock_broadcast.assert_awaited_once()
        guild_arg, payload = mock_broadcast.call_args.args
        assert guild_arg == "g-ensure1"
        assert payload["type"] == "thread-created"
        assert payload["threadId"] == thread.id

    async def test_second_call_reuses_thread_without_rebroadcast(self, db_session):
        insert_guild(db_session, "g-ensure2")

        with patch("foreman.thread_service.broadcast", new_callable=AsyncMock) as mock_broadcast:
            thread1 = await ensure_conversation_thread("g-ensure2", "user-1", "hi")
            mock_broadcast.reset_mock()
            thread2 = await ensure_conversation_thread("g-ensure2", "user-1", "again")

        assert thread1.id == thread2.id
        mock_broadcast.assert_not_awaited()

    async def test_unknown_guild_returns_none_without_raising(self, db_session):
        with patch("foreman.thread_service.broadcast", new_callable=AsyncMock) as mock_broadcast:
            thread = await ensure_conversation_thread("g-does-not-exist", "user-1", "hi")

        assert thread is None
        mock_broadcast.assert_not_awaited()


# ── broadcast_thread_updated ─────────────────────────────────────────────────


class TestBroadcastThreadUpdated:
    async def test_broadcasts_to_owning_guild(self, db_session):
        insert_guild(db_session, "g-updated1")
        guild_pk = await _guild_pk("g-updated1")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            thread.status = "archived"
            db.add(thread)
            await db.commit()
            await db.refresh(thread)

            with patch(
                "foreman.thread_service.broadcast", new_callable=AsyncMock
            ) as mock_broadcast:
                await broadcast_thread_updated(db, thread)

        mock_broadcast.assert_awaited_once()
        guild_arg, payload = mock_broadcast.call_args.args
        assert guild_arg == "g-updated1"
        assert payload["type"] == "thread-updated"
        assert payload["threadId"] == thread.id
        assert payload["status"] == "archived"

    async def test_swallows_broadcast_failure(self, db_session):
        """A WS hiccup must never propagate — thread bookkeeping is best-effort."""
        insert_guild(db_session, "g-updated2")
        guild_pk = await _guild_pk("g-updated2")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

            with patch(
                "foreman.thread_service.broadcast",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ws down"),
            ):
                await broadcast_thread_updated(db, thread)  # must not raise


# ── Conversation mirroring (issue #1274) ─────────────────────────────────────


class TestConversationMirroring:
    async def test_creating_a_thread_mirrors_its_fields_onto_conversation(self, db_session):
        insert_guild(db_session, "g-mirror1")
        guild_pk = await _guild_pk("g-mirror1")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(
                db, guild_pk, "user-1", name_hint="hello there"
            )
            conversation = await db.get(Conversation, thread.conversation_id)

        assert conversation.name == "hello there"
        assert conversation.status == "active"
        assert conversation.discord_thread_id is None

    async def test_new_thread_after_archive_resets_conversation_to_active(self, db_session):
        """Mirrors TestGetOrCreateActiveThread.test_new_thread_created_when_previous_is_archived:
        once the old thread is archived directly (as the Discord-side
        archive/close routes do) and a fresh message rolls a new thread, the
        conversation's mirrored fields must reflect the *new* thread, not the
        stale archived one."""
        insert_guild(db_session, "g-mirror2")
        guild_pk = await _guild_pk("g-mirror2")

        async with database_module.AsyncSessionLocal() as db:
            old_thread, _ = await get_or_create_active_thread(
                db, guild_pk, "user-1", name_hint="first session"
            )
            old_thread.status = "archived"
            old_thread.discord_thread_id = "discord-old"
            db.add(old_thread)
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            new_thread, created = await get_or_create_active_thread(
                db, guild_pk, "user-1", name_hint="second session"
            )
            conversation = await db.get(Conversation, new_thread.conversation_id)

        assert created is True
        assert conversation.status == "active"
        assert conversation.name == "second session"
        assert conversation.discord_thread_id is None

    async def test_sync_helper_skips_when_conversation_already_moved_on(self, db_session):
        """A stale (superseded) thread transitioning archived -> closed must
        not stomp a conversation that has already rolled to a newer active
        thread — see ``sync_conversation_after_thread_update``'s guard."""
        insert_guild(db_session, "g-mirror3")
        guild_pk = await _guild_pk("g-mirror3")

        async with database_module.AsyncSessionLocal() as db:
            old_thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            old_thread.status = "archived"
            db.add(old_thread)
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            # Conversation rolls to a brand-new active thread.
            await get_or_create_active_thread(db, guild_pk, "user-1", name_hint="new session")

        async with database_module.AsyncSessionLocal() as db:
            old_thread = await db.get(Thread, old_thread.id)
            old_thread.status = "closed"
            db.add(old_thread)
            # The guard should refuse to overwrite: conversation.status is
            # "active" (the new thread), not "archived" (old_thread's
            # pre-transition status).
            await sync_conversation_after_thread_update(db, old_thread, previous_status="archived")
            await db.commit()

            conversation = await db.get(Conversation, old_thread.conversation_id)

        assert conversation.status == "active"
        assert conversation.name == "new session"


# ── reactivate_conversation_thread (issue #1278) ─────────────────────────────


class TestReactivateConversationThread:
    async def test_reactivates_archived_thread_and_unarchives_discord(self, db_session):
        insert_guild(db_session, "g-react1")
        guild_pk = await _guild_pk("g-react1")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            thread.discord_thread_id = "discord-thread-react"
            thread.status = "archived"
            db.add(thread)
            conversation = await db.get(Conversation, thread.conversation_id)
            conversation.discord_thread_id = "discord-thread-react"
            conversation.status = "archived"
            db.add(conversation)
            await db.commit()

        with patch(
            "discord.thread_mirror.on_thread_updated", new_callable=AsyncMock
        ) as mock_unarchive:
            async with database_module.AsyncSessionLocal() as db:
                conversation = await db.get(Conversation, conversation.id)
                reactivated = await reactivate_conversation_thread(
                    db, conversation, "discord-thread-react"
                )

        assert reactivated is not None
        assert reactivated.id == thread.id
        assert reactivated.status == "active"
        mock_unarchive.assert_called_once_with(thread_id=thread.id, status="active")

        async with database_module.AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, conversation.id)
        assert conversation.status == "active"

    async def test_noop_discord_call_when_thread_already_active(self, db_session):
        insert_guild(db_session, "g-react2")
        guild_pk = await _guild_pk("g-react2")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            thread.discord_thread_id = "discord-thread-react2"
            db.add(thread)
            conversation = await db.get(Conversation, thread.conversation_id)
            conversation.discord_thread_id = "discord-thread-react2"
            db.add(conversation)
            await db.commit()

        with patch(
            "discord.thread_mirror.on_thread_updated", new_callable=AsyncMock
        ) as mock_unarchive:
            async with database_module.AsyncSessionLocal() as db:
                conversation = await db.get(Conversation, conversation.id)
                reactivated = await reactivate_conversation_thread(
                    db, conversation, "discord-thread-react2"
                )

        assert reactivated.status == "active"
        mock_unarchive.assert_not_called()

    async def test_returns_none_when_no_matching_thread_row(self, db_session):
        insert_guild(db_session, "g-react3")
        guild_pk = await _guild_pk("g-react3")

        async with database_module.AsyncSessionLocal() as db:
            conversation = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, conversation.id)
            reactivated = await reactivate_conversation_thread(
                db, conversation, "no-such-discord-thread"
            )

        assert reactivated is None
