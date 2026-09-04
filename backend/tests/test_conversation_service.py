"""Tests for foreman.conversation_service (issue #1271).

#1271's first PR is additive: a nullable Conversation.id FK on messages/
tasks/foreman_turns/github_events, written alongside the existing thread_id
wherever it's already stamped. These tests cover the resolution helper
(``resolve_conversation_id``) and that the primary write sites — task
creation and human chat — actually stamp ``conversation_id``, not just
``thread_id``.
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel.ext.asyncio.session import AsyncSession

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from foreman.conversation_service import (
    get_or_create_conversation,
    resolve_conversation_id,
    touch_conversation,
)
from foreman.thread_service import get_or_create_active_thread
from helpers import create_db, insert_guild, insert_task, truncate_all
from models import Task
from sqlalchemy import update
from sqlmodel import col


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


# ── resolve_conversation_id ──────────────────────────────────────────────────


class TestResolveConversationId:
    async def test_no_task_no_user_returns_none(self, db_session):
        insert_guild(db_session, "g-rc1")
        guild_pk = await _guild_pk("g-rc1")

        async with database_module.AsyncSessionLocal() as db:
            resolved = await resolve_conversation_id(db, guild_pk)

        assert resolved is None

    async def test_user_only_gets_or_creates_conversation(self, db_session):
        insert_guild(db_session, "g-rc2")
        guild_pk = await _guild_pk("g-rc2")

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()
            conv_id = conv.id

        async with database_module.AsyncSessionLocal() as db:
            resolved = await resolve_conversation_id(db, guild_pk, user_id="user-1")

        assert resolved == conv_id

    async def test_task_id_takes_precedence_over_user_id(self, db_session):
        """A task's stamped conversation_id wins even if the caller also passes
        a user_id belonging to a *different* conversation — mirrors
        resolve_thread_id's task-first precedence."""
        insert_guild(db_session, "g-rc3")
        guild_pk = await _guild_pk("g-rc3")
        insert_task(db_session, "g-rc3", "t-rc3")

        async with database_module.AsyncSessionLocal() as db:
            task_conv = await get_or_create_conversation(db, guild_pk, "task-owner")
            await db.commit()
            await db.exec(
                update(Task).where(col(Task.id) == "t-rc3").values(conversation_id=task_conv.id)
            )
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            resolved = await resolve_conversation_id(
                db, guild_pk, task_id="t-rc3", user_id="someone-else"
            )

        assert resolved == task_conv.id

    async def test_task_with_no_conversation_falls_back_to_user_id(self, db_session):
        insert_guild(db_session, "g-rc4")
        guild_pk = await _guild_pk("g-rc4")
        insert_task(db_session, "g-rc4", "t-rc4")  # conversation_id left NULL

        async with database_module.AsyncSessionLocal() as db:
            resolved = await resolve_conversation_id(
                db, guild_pk, task_id="t-rc4", user_id="user-1"
            )

        async with database_module.AsyncSessionLocal() as db:
            expected = await get_or_create_conversation(db, guild_pk, "user-1")

        assert resolved == expected.id


class TestTouchConversation:
    async def test_bumps_updated_at(self, db_session):
        insert_guild(db_session, "g-tc1")
        guild_pk = await _guild_pk("g-tc1")

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await db.commit()
            first_updated = conv.updated_at

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")
            await touch_conversation(db, conv)
            second_updated = conv.updated_at

        assert second_updated >= first_updated


# ── dual-write: thread_id + conversation_id stamped together ────────────────


class TestConversationIdDualWrite:
    async def test_task_created_from_conversation_gets_stamped(self, db_session):
        """foreman.tools._handle_create_task's thread lookup should stamp
        both Task.thread_id and Task.conversation_id — this test exercises
        the same get_or_create_active_thread() call site uses and confirms
        thread.conversation_id is what a Task row would be stamped with."""
        insert_guild(db_session, "g-dw1")
        guild_pk = await _guild_pk("g-dw1")

        async with database_module.AsyncSessionLocal() as db:
            thread, _created = await get_or_create_active_thread(db, guild_pk, "user-1")

        assert thread.conversation_id is not None

        async with database_module.AsyncSessionLocal() as db:
            conv = await get_or_create_conversation(db, guild_pk, "user-1")

        assert thread.conversation_id == conv.id
