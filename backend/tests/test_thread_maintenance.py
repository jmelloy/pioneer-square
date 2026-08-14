"""Tests for the periodic thread-health sweep (issue #1167).

``sweep_threads`` is the Foreman-side periodic mechanism that ages threads
along their lifecycle (active -> archived -> closed) and unlinks tasks from
threads that are no longer live. Unlike the old, corrected-away Discord
Gateway sync (see epic #1160's "Architectural correction" comment), this
sweep is driven entirely by the Foreman's own periodic-check cycle — it
never reacts to Discord events.
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
import foreman.thread_maintenance as thread_maintenance
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from foreman.thread_service import get_or_create_active_thread
from helpers import create_db, insert_guild, insert_task, truncate_all
from models import Task, Thread


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    # Keep the sweep from actually archiving/closing anything unless a test
    # explicitly lowers these below zero — real defaults are days-long.
    monkeypatch.setattr(thread_maintenance, "THREAD_AUTO_ARCHIVE_AFTER_SECONDS", 3 * 24 * 3600.0)
    monkeypatch.setattr(thread_maintenance, "THREAD_AUTO_CLOSE_AFTER_SECONDS", 14 * 24 * 3600.0)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


async def _guild_pk(guild_id: str) -> int:
    async with database_module.AsyncSessionLocal() as db:
        pk = await get_guild_pk(db, guild_id)
    assert pk is not None
    return pk


async def _backdate_thread(thread_id: str, updated_at: datetime) -> None:
    async with database_module.AsyncSessionLocal() as db:
        thread = await db.get(Thread, thread_id)
        thread.updated_at = updated_at
        db.add(thread)
        await db.commit()


class TestSweepThreads:
    async def test_leaves_fresh_active_thread_untouched(self, db_session):
        insert_guild(db_session, "g-sweep-fresh")
        guild_pk = await _guild_pk("g-sweep-fresh")
        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")

        summary = await thread_maintenance.sweep_threads("g-sweep-fresh")

        assert summary == {"archived": 0, "closed": 0, "orphaned_cleaned": 0}
        async with database_module.AsyncSessionLocal() as db:
            refreshed = await db.get(Thread, thread.id)
        assert refreshed.status == "active"

    async def test_archives_stale_active_thread(self, db_session):
        insert_guild(db_session, "g-sweep-archive")
        guild_pk = await _guild_pk("g-sweep-archive")
        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
        await _backdate_thread(thread.id, datetime.now(UTC) - timedelta(days=4))

        with patch("discord_notifier.is_configured", return_value=False):
            summary = await thread_maintenance.sweep_threads("g-sweep-archive")

        assert summary == {"archived": 1, "closed": 0, "orphaned_cleaned": 0}
        async with database_module.AsyncSessionLocal() as db:
            refreshed = await db.get(Thread, thread.id)
        assert refreshed.status == "archived"

    async def test_closes_stale_archived_thread(self, db_session):
        insert_guild(db_session, "g-sweep-close")
        guild_pk = await _guild_pk("g-sweep-close")
        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            thread.status = "archived"
            db.add(thread)
            await db.commit()
        await _backdate_thread(thread.id, datetime.now(UTC) - timedelta(days=15))

        with patch("discord_notifier.is_configured", return_value=False):
            summary = await thread_maintenance.sweep_threads("g-sweep-close")

        assert summary == {"archived": 0, "closed": 1, "orphaned_cleaned": 0}
        async with database_module.AsyncSessionLocal() as db:
            refreshed = await db.get(Thread, thread.id)
        assert refreshed.status == "closed"

    async def test_unlinks_non_terminal_task_from_closed_thread(self, db_session):
        insert_guild(db_session, "g-sweep-orphan")
        guild_pk = await _guild_pk("g-sweep-orphan")
        insert_task(db_session, "g-sweep-orphan", "t-orphan1", state="pending")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            thread.status = "closed"
            db.add(thread)
            task = await db.get(Task, "t-orphan1")
            task.thread_id = thread.id
            db.add(task)
            await db.commit()

        with patch("discord_notifier.is_configured", return_value=False):
            summary = await thread_maintenance.sweep_threads("g-sweep-orphan")

        assert summary["orphaned_cleaned"] == 1
        async with database_module.AsyncSessionLocal() as db:
            task = await db.get(Task, "t-orphan1")
        assert task.thread_id is None

    async def test_leaves_terminal_task_linked_to_closed_thread(self, db_session):
        """A finished task's thread link is historical record, not live
        routing — the sweep only unlinks non-terminal (still-active) tasks."""
        insert_guild(db_session, "g-sweep-terminal")
        guild_pk = await _guild_pk("g-sweep-terminal")
        insert_task(db_session, "g-sweep-terminal", "t-done1", state="done")

        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
            thread.status = "closed"
            db.add(thread)
            task = await db.get(Task, "t-done1")
            task.thread_id = thread.id
            db.add(task)
            await db.commit()

        with patch("discord_notifier.is_configured", return_value=False):
            summary = await thread_maintenance.sweep_threads("g-sweep-terminal")

        assert summary["orphaned_cleaned"] == 0
        async with database_module.AsyncSessionLocal() as db:
            task = await db.get(Task, "t-done1")
        assert task.thread_id == thread.id

    async def test_unknown_guild_returns_zeroed_summary(self, db_session):
        summary = await thread_maintenance.sweep_threads("g-does-not-exist")
        assert summary == {"archived": 0, "closed": 0, "orphaned_cleaned": 0}

    async def test_notifies_discord_only_when_something_changed(self, db_session):
        insert_guild(db_session, "g-sweep-notify")
        guild_pk = await _guild_pk("g-sweep-notify")
        async with database_module.AsyncSessionLocal() as db:
            thread, _ = await get_or_create_active_thread(db, guild_pk, "user-1")
        await _backdate_thread(thread.id, datetime.now(UTC) - timedelta(days=4))

        with (
            patch("discord_notifier.is_configured", return_value=True),
            patch(
                "discord_notifier.notify", new_callable=AsyncMock
            ) as mock_notify,
        ):
            await thread_maintenance.sweep_threads("g-sweep-notify")

        mock_notify.assert_awaited_once()
        assert mock_notify.call_args.kwargs["ps_guild_slug"] == "g-sweep-notify"

    async def test_quiet_sweep_does_not_notify_discord(self, db_session):
        insert_guild(db_session, "g-sweep-quiet")
        guild_pk = await _guild_pk("g-sweep-quiet")
        async with database_module.AsyncSessionLocal() as db:
            await get_or_create_active_thread(db, guild_pk, "user-1")

        with (
            patch("discord_notifier.is_configured", return_value=True),
            patch(
                "discord_notifier.notify", new_callable=AsyncMock
            ) as mock_notify,
        ):
            await thread_maintenance.sweep_threads("g-sweep-quiet")

        mock_notify.assert_not_awaited()

    async def test_sweep_never_raises_on_db_error(self, db_session, monkeypatch):
        async def _boom(*args, **kwargs):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(thread_maintenance, "_sweep_threads_once", _boom)

        summary = await thread_maintenance.sweep_threads("g-anything")

        assert summary == {"archived": 0, "closed": 0, "orphaned_cleaned": 0}
