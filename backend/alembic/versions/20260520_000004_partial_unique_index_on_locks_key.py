"""partial_unique_index_on_locks_key

Recreate the locks table with an auto-increment id PK (dropping key as PK),
proper TIMESTAMPTZ columns for acquired_at and expires_at, and a unique
index on key so only one lock per key can exist at a time.

LockService.acquire() always deletes expired locks for a key before inserting,
so a simple unique index enforces the same "one active lock" invariant without
needing now() in the predicate (which PostgreSQL rejects because now() is not
IMMUTABLE).

Revision ID: 20260520_000004_partial_unique_index_on_locks_key
Revises: 20260520_000003_merge_locking_and_messages
Create Date: 2026-05-20 00:00:04.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000004_partial_unique_index_on_locks_key"
down_revision: str | Sequence[str] | None = "20260520_000004_nullable_task_worker_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=True),
    )
