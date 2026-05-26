"""Standalone key-value lock table.

Locks are acquired with a TTL so stale entries expire automatically even when
the explicit release code path is skipped.  Default TTL is 30 minutes, shorter
than the previous 1-hour stale-cutoff while still generous enough to cover
normal follow-up round-trips.

Lock keys use namespaced strings: ``task:<task_id>``, ``worker:<worker_id>``, etc.

Timestamps are stored as timezone-aware UTC datetimes (TIMESTAMPTZ columns).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from models import Lock
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession


class LockService:
    DEFAULT_TTL_SECONDS = 1800  # 30 minutes

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def acquire(self, key: str, owner: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
        """Try to acquire *key*. Returns True if the lock was granted, False if already held."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds)

        # Evict any expired lock for this key before attempting the insert.
        await self._db.execute(
            delete(Lock).where(
                Lock.key == key,
                Lock.expires_at.isnot(None),
                Lock.expires_at < now,
            )
        )

        # Use a savepoint so an IntegrityError from the partial unique index
        # (locks_key_active_unique) rolls back only the insert, leaving the
        # expired-lock eviction above intact.
        try:
            async with self._db.begin_nested():
                await self._db.execute(
                    insert(Lock).values(
                        key=key, owner=owner, acquired_at=now, expires_at=expires_at
                    )
                )
            return True
        except IntegrityError:
            return False

    async def release(self, key: str) -> None:
        """Release *key*. Safe to call even if the lock is not held (idempotent)."""
        await self._db.execute(delete(Lock).where(Lock.key == key))

    async def is_locked(self, key: str) -> bool:
        """Return True if a non-expired lock exists for *key*."""
        now = datetime.now(UTC)
        row = await self._db.exec(
            select(Lock.key).where(
                Lock.key == key,
                (Lock.expires_at.is_(None) | (Lock.expires_at > now)),
            )
        )
        # SQLModel's ScalarResult.one_or_none() on a scalar-column query returns
        # the value directly (str or None), equivalent to .scalar_one_or_none().
        return row.one_or_none() is not None

    @staticmethod
    async def cleanup_expired(db: AsyncSession) -> int:
        """Delete all expired locks. Returns the number of rows removed."""
        now = datetime.now(UTC)
        result = await db.execute(
            delete(Lock).where(Lock.expires_at.isnot(None), Lock.expires_at < now)
        )
        return result.rowcount
