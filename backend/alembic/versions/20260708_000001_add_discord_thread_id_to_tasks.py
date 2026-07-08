"""Add discord_thread_id column to tasks.

Revision ID: 20260708_000001_add_discord_thread_id_to_tasks
Revises: 20260708_000000_add_issue_phase_and_index
Create Date: 2026-07-08

Part of Discord thread routing for the issue-rooted task tree (#832). A
phase='issue' root task stores its Discord thread ID directly on this
column at creation time; child tasks (plan/execute/review/followup) resolve
their thread by walking parent_task_id up to the root instead of each phase
creating (or looking up) its own thread. NULL on every non-root task and on
legacy tasks created before this column existed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260708_000001_add_discord_thread_id_to_tasks"
down_revision: str | Sequence[str] | None = "20260708_000000_add_issue_phase_and_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("discord_thread_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "discord_thread_id")
