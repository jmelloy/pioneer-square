"""Tests for the periodic-check narration guard (issue #1314).

Before this guard, ``foreman.runner._poll_loop`` fired a
``[periodic-check]`` foreman run into active conversations on *every*
cycle, even when there was nothing new to report (zero non-terminal tasks,
zero devReady issues). From inside one of those conversations this read as
a stuck loop endlessly repeating the same "no non-terminal tasks" status
message, bounded only by the (multi-hour, eventually 24h) backoff ceiling.

Exercises ``_poll_loop_once`` — the single-cycle worker ``_poll_loop``
delegates to — directly with one plain ``await``. No background task,
cancellation, or ``asyncio.sleep`` patching is involved: those would touch
process-global asyncio state shared with every other concurrently-running
coroutine (including other tests' leftover background tasks under
pytest-asyncio's shared event loop), which is exactly the kind of
cross-test flakiness this file used to hit in CI.
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
import foreman.runner as runner
from _test_config import TEST_DATABASE_URL
from auth_deps import get_guild_pk
from helpers import create_db, insert_guild, insert_task, truncate_all
from models import Conversation


@pytest.fixture()
def db_session(monkeypatch):
    """Provide the PostgreSQL test database, isolated per test."""
    create_db(TEST_DATABASE_URL)
    truncate_all(TEST_DATABASE_URL)

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    # foreman.runner._load_foreman_config uses the name `AsyncSessionLocal`
    # imported directly into its own module namespace (`from database import
    # AsyncSessionLocal`), a separate binding from `database.AsyncSessionLocal`
    # above — patching only the latter leaves it pointed at the real
    # production sessionmaker, whose engine ends up bound to whichever
    # test's event loop touches it first and breaks every test after with
    # "Future attached to a different loop".
    monkeypatch.setattr(runner, "AsyncSessionLocal", session_factory)

    yield TEST_DATABASE_URL


async def _guild_pk(guild_id: str) -> int:
    async with database_module.AsyncSessionLocal() as db:
        pk = await get_guild_pk(db, guild_id)
    assert pk is not None
    return pk


async def _insert_active_conversation(guild_id: str, user_id: str) -> None:
    from datetime import UTC, datetime

    guild_pk = await _guild_pk(guild_id)
    now = datetime.now(UTC)
    async with database_module.AsyncSessionLocal() as db:
        db.add(
            Conversation(
                guild_id=guild_pk, user_id=user_id, status="active", created_at=now, updated_at=now
            )
        )
        await db.commit()


async def _run_one_poll_cycle(guild_id: str, spawned: list) -> float:
    """Run exactly one ``_poll_loop_once`` cycle, patching out every
    collaborator that would otherwise hit the network (GitHub, models.dev)
    or actually run the foreman AI — none of those are what this test is
    about."""

    def _fake_spawn(coro, *, name=None):
        spawned.append((name, coro))
        coro.close()  # never actually run the foreman AI in this test

    with (
        patch("foreman.runner.spawn", side_effect=_fake_spawn),
        patch("util.models_dev.refresh_model_catalog_if_stale", AsyncMock(return_value=False)),
        patch("foreman.thread_maintenance.sweep_threads", AsyncMock(return_value={})),
        patch("foreman.runner.broadcast_msg", AsyncMock()),
    ):
        return await runner._poll_loop_once(guild_id, runner.POLL_MIN_SECS)


class TestPollLoopNarrationGuard:
    async def test_skips_narration_when_nothing_to_report(self, db_session):
        insert_guild(db_session, "g-poll-quiet")
        await _insert_active_conversation("g-poll-quiet", "user-1")

        spawned: list = []
        await _run_one_poll_cycle("g-poll-quiet", spawned)

        assert spawned == []

    async def test_fires_narration_when_a_task_is_active(self, db_session):
        insert_guild(db_session, "g-poll-busy")
        await _insert_active_conversation("g-poll-busy", "user-1")
        insert_task(db_session, "g-poll-busy", "t-busy1", state="working")

        spawned: list = []
        await _run_one_poll_cycle("g-poll-busy", spawned)

        assert len(spawned) == 1
        name, _coro = spawned[0]
        assert name == "foreman.poll:g-poll-busy"
