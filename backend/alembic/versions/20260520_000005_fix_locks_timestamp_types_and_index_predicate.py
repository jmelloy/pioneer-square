"""fix_locks_timestamp_types_and_index_predicate

Recreate the locks table to fix migration 000004 which used now() in the
partial index predicate — PostgreSQL rejects this because now() is STABLE,
not IMMUTABLE.  Replace with a simple unique index on key; LockService.acquire()
already deletes expired rows before inserting, so correctness is preserved.

Revision ID: 20260520_000005_fix_locks_timestamp_types_and_index_predicate
Revises: 20260520_000004_partial_unique_index_on_locks_key
Create Date: 2026-05-20 00:00:05.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000005_fix_locks_timestamp_types_and_index_predicate"
down_revision: str | Sequence[str] | None = "20260520_000004_partial_unique_index_on_locks_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Locks are ephemeral; safe to drop and recreate (any live locks will simply
    # be re-acquired on the next heartbeat/retry cycle).
    op.drop_table("locks")
    op.create_table(
        "locks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("CREATE UNIQUE INDEX locks_key_unique ON locks (key)")


def downgrade() -> None:
    op.drop_table("locks")
    op.create_table(
        "locks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("CREATE UNIQUE INDEX locks_key_unique ON locks (key)")
