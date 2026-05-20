"""Standalone key-value lock table.

Locks are acquired with a TTL so stale entries expire automatically even when
the explicit release code path is skipped.  Default TTL is 30 minutes, shorter
than the previous 1-hour stale-cutoff while still generous enough to cover
normal follow-up round-trips.

Lock keys use namespaced strings: ``task:<task_id>``, ``worker:<worker_id>``, etc.

Timestamps are stored as UTC ISO-8601 strings without a timezone suffix
(``2026-05-20T12:34:56.789012``).  Omitting the ``+00:00`` suffix keeps
lexicographic ordering unambiguous and avoids any inconsistency between
Python versions that may format the offset differently (``+00:00`` vs ``Z``).
All timestamp comparisons in this module use the same helper so the format
is always consistent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from models import Lock
from sqlalchemy import delete, select
from sqlalchemy import insert as sa_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _utc_iso() -> str:
    """Return current UTC time as a timezone-free ISO-8601 string.

    Strips the timezone offset so stored timestamps compare correctly via
    plain lexicographic ordering regardless of Python/platform differences
    in how the ``+00:00`` / ``Z`` suffix is formatted.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")


class LockService:
    DEFAULT_TTL_SECONDS = 1800  # 30 minutes

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def acquire(self, key: str, owner: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
        """Try to acquire *key*. Returns True if the lock was granted, False if already held."""
        now_iso = _utc_iso()
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )

        # Evict any expired lock for this key before attempting the insert.
        await self._db.execute(
            delete(Lock).where(
                Lock.key == key,
                Lock.expires_at.isnot(None),
                Lock.expires_at < now_iso,
            )
        )

        # Use a savepoint so a primary-key conflict rolls back only this
        # attempt without aborting the enclosing transaction.  The only
        # IntegrityError expected here is a UNIQUE/PK violation on ``key``
        # (meaning another caller holds the lock); all other constraint
        # errors are re-raised.
        try:
            async with self._db.begin_nested():
                await self._db.execute(
                    sa_insert(Lock).values(
                        key=key, owner=owner, acquired_at=now_iso, expires_at=expires_at
                    )
                )
            return True
        except IntegrityError as exc:
            # Only swallow primary-key / UNIQUE conflicts.  Any other
            # constraint violation (e.g. a NULL in a NOT NULL column) is
            # a programming error and should propagate.
            orig_msg = str(exc.orig).lower() if exc.orig else ""
            if "unique" not in orig_msg and "primary key" not in orig_msg:
                raise
            return False

    async def release(self, key: str) -> None:
        """Release *key*. Safe to call even if the lock is not held (idempotent)."""
        await self._db.execute(delete(Lock).where(Lock.key == key))

    async def is_locked(self, key: str) -> bool:
        """Return True if a non-expired lock exists for *key*."""
        now_iso = _utc_iso()
        row = await self._db.execute(
            select(Lock.key).where(
                Lock.key == key,
                (Lock.expires_at.is_(None) | (Lock.expires_at > now_iso)),
            )
        )
        return row.scalar_one_or_none() is not None

    @staticmethod
    async def cleanup_expired(db: AsyncSession) -> int:
        """Delete all expired locks. Returns the number of rows removed."""
        now_iso = _utc_iso()
        result = await db.execute(
            delete(Lock).where(Lock.expires_at.isnot(None), Lock.expires_at < now_iso)
        )
        return result.rowcount
