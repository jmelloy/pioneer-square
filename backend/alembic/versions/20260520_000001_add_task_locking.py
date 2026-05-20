"""add_task_locking

Add locked_at / lock_holder to the tasks table and create the task_events
table to queue follow-up triggers that arrive while a task is locked.

Revision ID: 20260520_000001_add_task_locking
Revises: 20260520_000000_add_index_worker_last_seen
Create Date: 2026-05-20 00:00:01.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000001_add_task_locking"
down_revision: str | Sequence[str] | None = "20260520_000000_add_index_worker_last_seen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("locked_at", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("lock_holder", sa.Text(), nullable=True))

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_events_task_id", "task_events")
    op.drop_table("task_events")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("lock_holder")
        batch_op.drop_column("locked_at")
