"""Tests for the periodic-check narration guard (issue #1314).

Before this guard, ``foreman.runner._poll_loop`` fired a
``[periodic-check]`` foreman run into every active conversation on *every*
cycle, even when there was nothing new to report (zero non-terminal tasks,
zero devReady issues). From inside one of those conversations this read as
a stuck loop endlessly repeating the same "no non-terminal tasks" status
message, bounded only by the (multi-hour, eventually 24h) backoff ceiling.
"""

from __future__ import annotations

import asyncio
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


async def _run_one_poll_iteration(guild_id: str, spawned: list) -> None:
    """Run exactly one ``_poll_loop`` iteration, then stop it.

    Patches out ``asyncio.sleep`` (fires instantly — never raises, since
    ``asyncio.sleep`` is a single global function shared by every coroutine
    in the process, including unrelated library internals, so anything that
    counts calls or raises from it is a trap for cross-test flakiness) and
    every collaborator that would otherwise hit the network (GitHub,
    models.dev). ``broadcast_msg`` — called exactly once, at the very end of
    each successful iteration, right before the loop goes back around for
    another sleep — is used as the deterministic "one iteration finished"
    signal: its side effect cancels the loop's own task, which
    ``_poll_loop`` catches via its ``except asyncio.CancelledError: return``
    around the next sleep.
    """

    def _fake_spawn(coro, *, name=None):
        spawned.append((name, coro))
        coro.close()  # never actually run the foreman AI in this test

    with (
        patch("foreman.runner.asyncio.sleep", AsyncMock(return_value=None)),
        patch("foreman.runner.spawn", side_effect=_fake_spawn),
        patch("util.models_dev.refresh_model_catalog_if_stale", AsyncMock(return_value=False)),
        patch("foreman.thread_maintenance.sweep_threads", AsyncMock(return_value={})),
        patch("foreman.runner.broadcast_msg", AsyncMock()) as mock_broadcast_msg,
    ):
        runner._poll_tasks.pop(guild_id, None)
        task = asyncio.create_task(runner._poll_loop(guild_id))
        runner._poll_tasks[guild_id] = task
        mock_broadcast_msg.side_effect = lambda *a, **kw: task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pass
        finally:
            runner._poll_tasks.pop(guild_id, None)


class TestPollLoopNarrationGuard:
    async def test_skips_narration_when_nothing_to_report(self, db_session):
        insert_guild(db_session, "g-poll-quiet")
        await _insert_active_conversation("g-poll-quiet", "user-1")

        spawned: list = []
        await _run_one_poll_iteration("g-poll-quiet", spawned)

        assert spawned == []

    async def test_fires_narration_when_a_task_is_active(self, db_session):
        insert_guild(db_session, "g-poll-busy")
        await _insert_active_conversation("g-poll-busy", "user-1")
        insert_task(db_session, "g-poll-busy", "t-busy1", state="working")

        spawned: list = []
        await _run_one_poll_iteration("g-poll-busy", spawned)

        assert len(spawned) == 1
        name, _coro = spawned[0]
        assert name == "foreman.poll:g-poll-busy:user-1"
