"""add_locks_table

Create the standalone ``locks`` key-value table and remove the now-redundant
``locked_at`` / ``lock_holder`` columns from ``tasks``.  Lock lifetime is
independent of the task row; TTL-based expiry ensures stale locks are cleaned
up automatically even if the explicit release path is skipped.

Revision ID: 20260520_000002_add_locks_table
Revises: 20260520_000001_add_task_locking
Create Date: 2026-05-20 00:00:02.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000002_add_locks_table"
down_revision: str | Sequence[str] | None = "20260520_000001_add_task_locking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "locks",
        sa.Column("key", sa.Text(), primary_key=True, nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=True),
    )
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("locked_at")
        batch_op.drop_column("lock_holder")


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("locked_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("lock_holder", sa.Text(), nullable=True))
    op.drop_table("locks")
