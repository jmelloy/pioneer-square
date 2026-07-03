"""Add discord_threads table for per-issue/PR Discord thread persistence.

Revision ID: 20260703_000000_add_discord_threads
Revises: 20260701_000003_add_issue_state_to_tasks
Create Date: 2026-07-03

Maps GitHub issue/PR coordinates (issue_repo, issue_number) to a Discord
thread channel ID so the notifier can route subsequent events into the same
thread without re-creating it on restart.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260703_000000_add_discord_threads"
down_revision: str | Sequence[str] | None = "20260701_000003_add_issue_state_to_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_repo", sa.Text(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_threads_repo_number",
        "discord_threads",
        ["issue_repo", "issue_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_discord_threads_repo_number", table_name="discord_threads")
    op.drop_table("discord_threads")
