"""Unit tests for LockService.

Covers: acquire → locked, release → free, TTL expiry, double-acquire fails,
is_locked, and cleanup_expired.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import database as database_module
from helpers import create_db
from lock_service import LockService


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_locks.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DB_PATH", db_path)
    create_db(db_path)
    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", session_factory)
    yield session_factory


@pytest.mark.anyio
async def test_acquire_succeeds_when_free(db_session):
    async with db_session() as db:
        svc = LockService(db)
        acquired = await svc.acquire("task:t-001", owner="w-abc")
        await db.commit()
    assert acquired is True


@pytest.mark.anyio
async def test_double_acquire_fails(db_session):
    async with db_session() as db:
        svc = LockService(db)
        first = await svc.acquire("task:t-002", owner="w-first")
        await db.commit()
        second = await svc.acquire("task:t-002", owner="w-second")
        await db.commit()
    assert first is True
    assert second is False


@pytest.mark.anyio
async def test_release_allows_reacquire(db_session):
    async with db_session() as db:
        svc = LockService(db)
        await svc.acquire("task:t-003", owner="w-first")
        await db.commit()
        await svc.release("task:t-003")
        await db.commit()
        reacquired = await svc.acquire("task:t-003", owner="w-second")
        await db.commit()
    assert reacquired is True


@pytest.mark.anyio
async def test_release_is_idempotent(db_session):
    async with db_session() as db:
        svc = LockService(db)
        # Release a key that was never locked — must not raise.
        await svc.release("task:t-never-locked")
        await db.commit()
        # Double-release also safe.
        await svc.acquire("task:t-004", owner="w-x")
        await db.commit()
        await svc.release("task:t-004")
        await svc.release("task:t-004")
        await db.commit()


@pytest.mark.anyio
async def test_is_locked_true_after_acquire(db_session):
    async with db_session() as db:
        svc = LockService(db)
        await svc.acquire("task:t-005", owner="w-x")
        await db.commit()
        locked = await svc.is_locked("task:t-005")
    assert locked is True


@pytest.mark.anyio
async def test_is_locked_false_after_release(db_session):
    async with db_session() as db:
        svc = LockService(db)
        await svc.acquire("task:t-006", owner="w-x")
        await db.commit()
        await svc.release("task:t-006")
        await db.commit()
        locked = await svc.is_locked("task:t-006")
    assert locked is False


@pytest.mark.anyio
async def test_expired_lock_can_be_reacquired(db_session):
    """A lock with an already-elapsed TTL should be treated as free on the next acquire."""
    expired_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    async with db_session() as db:
        # Insert expired lock directly via raw SQL to bypass the service layer.
        await db.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO locks (key, owner, acquired_at, expires_at) "
                "VALUES ('task:t-007', 'w-old', :acq, :exp)"
            ),
            {"acq": expired_ts, "exp": expired_ts},
        )
        await db.commit()

        svc = LockService(db)
        acquired = await svc.acquire("task:t-007", owner="w-new")
        await db.commit()

    assert acquired is True


@pytest.mark.anyio
async def test_cleanup_expired_removes_stale_locks(db_session):
    import sqlalchemy

    expired_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    future_ts = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    now_ts = datetime.now(UTC).isoformat()

    async with db_session() as db:
        # Insert one expired and one active lock.
        await db.execute(
            sqlalchemy.text(
                "INSERT INTO locks (key, owner, acquired_at, expires_at) VALUES "
                "('task:stale', 'w-s', :now, :exp), "
                "('task:active', 'w-a', :now, :fut)"
            ),
            {"now": now_ts, "exp": expired_ts, "fut": future_ts},
        )
        await db.commit()

        removed = await LockService.cleanup_expired(db)
        await db.commit()

        remaining = (await db.execute(sqlalchemy.text("SELECT COUNT(*) FROM locks"))).scalar_one()

    assert removed == 1
    assert remaining == 1  # only the active lock survives


@pytest.mark.anyio
async def test_concurrent_acquire_only_one_wins(db_session):
    """Simulate two concurrent acquire calls — exactly one should succeed."""

    async def try_acquire(key: str, owner: str) -> bool:
        async with db_session() as db:
            svc = LockService(db)
            result = await svc.acquire(key, owner=owner)
            await db.commit()
            return result

    results = await asyncio.gather(
        try_acquire("task:t-race", "w-one"),
        try_acquire("task:t-race", "w-two"),
    )
    assert sorted(results) == [False, True]
