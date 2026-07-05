"""Add discord_task_threads table for per-task live-stream threads.

Revision ID: 20260705_000001_add_discord_task_threads
Revises: 20260705_000000_add_source_to_messages
Create Date: 2026-07-05

Maps a task_id to a Discord thread channel ID so a worker task's streaming
terminal output can be mirrored into a dedicated per-task thread while the
task is working — before any PR/issue thread exists — without re-creating the
thread on every batch or every restart.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260705_000001_add_discord_task_threads"
down_revision: str | Sequence[str] | None = "20260705_000000_add_source_to_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_task_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_task_threads_task_id",
        "discord_task_threads",
        ["task_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_discord_task_threads_task_id", table_name="discord_task_threads")
    op.drop_table("discord_task_threads")
