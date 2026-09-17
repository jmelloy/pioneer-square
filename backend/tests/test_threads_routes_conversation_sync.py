"""Tests for routes/threads.py mirroring Thread status onto Conversation (#1274).

``_set_status`` (the archive/close endpoints' shared implementation) is
covered directly rather than through the HTTP layer — the auth/HTTP wiring
is exercised elsewhere; this focuses on the new Conversation-mirroring side
effect.
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
from foreman.thread_service import (
    get_or_create_active_thread,
    sync_conversation_after_thread_update,
)
from helpers import create_db, insert_guild, truncate_all
from models import Conversation, Thread
from routes.threads import _set_status


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


class TestSetStatusMirrorsConversation:
    async def test_archiving_a_thread_mirrors_status_onto_conversation(self, db_session):
        insert_guild(db_session, "g-route-archive")
        guild_pk = await _guild_pk("g-route-archive")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

        async with database_module.AsyncSessionLocal() as db:
            out = await _set_status("g-route-archive", thread.id, "archived", "user-1", db)

        assert out.status == "archived"
        async with database_module.AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, thread.conversation_id)
        assert conversation.status == "archived"

    async def test_closing_an_already_superseded_thread_does_not_stomp_conversation(
        self, db_session
    ):
        """``get_or_create_active_thread`` now reactivates an archived thread
        instead of forking a new one (#1314), so "the conversation rolls to a
        brand-new active thread" has to be produced some other way here — a
        second thread created directly, the way the ``/threads`` REST
        endpoint does (``routes/threads.py:create_thread``)."""
        from datetime import UTC, datetime

        insert_guild(db_session, "g-route-stale")
        guild_pk = await _guild_pk("g-route-stale")

        async with database_module.AsyncSessionLocal() as db:
            old_thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

        async with database_module.AsyncSessionLocal() as db:
            await _set_status("g-route-stale", old_thread.id, "archived", "user-1", db)

        async with database_module.AsyncSessionLocal() as db:
            # The conversation rolls to a brand-new active thread (distinct
            # row, not a reactivation of old_thread) before the old one is
            # ever closed.
            now = datetime.now(UTC)
            fresh_thread = Thread(
                id="th-route-stale-fresh",
                conversation_id=old_thread.conversation_id,
                name="fresh",
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add(fresh_thread)
            await sync_conversation_after_thread_update(db, fresh_thread)
            await db.commit()

        async with database_module.AsyncSessionLocal() as db:
            await _set_status("g-route-stale", old_thread.id, "closed", "user-1", db)

        async with database_module.AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, old_thread.conversation_id)
        assert conversation.status == "active"
        assert conversation.name == "fresh"
