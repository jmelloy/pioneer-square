"""fix_locks_timestamp_types_and_index_predicate

Recreate the locks table to fix two issues introduced in migration 000004:
1. acquired_at and expires_at were stored as Text; change to TIMESTAMPTZ.
2. The partial unique index predicate included a 1-hour grace window after
   expiry. Tighten to: expires_at IS NULL OR expires_at > now().

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
    op.execute(
        """
        CREATE UNIQUE INDEX locks_key_active_unique
            ON locks (key)
            WHERE expires_at IS NULL OR expires_at > now()
        """
    )


def downgrade() -> None:
    op.drop_table("locks")
    op.create_table(
        "locks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=True),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX locks_key_active_unique
            ON locks (key)
            WHERE expires_at IS NULL
               OR expires_at > (now() - interval '1 hour')
        """
    )
