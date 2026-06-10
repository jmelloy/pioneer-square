"""Drop tasks.finished_at column.

Revision ID: 20260610_000000_drop_tasks_finished_at
Revises: 20260608_000000_rename_guild_slug
Create Date: 2026-06-10

Consolidates the finished_at and deleted_at columns into a single deleted_at column.
deleted_at already serves as the soft-delete expiry timestamp (set to now + 3 days on
completion); finished_at was a redundant "when did the task complete?" marker.

The completion time can be approximated as deleted_at - DEFAULT_TTL (3 days).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260610_000000_drop_tasks_finished_at"
down_revision: str | Sequence[str] | None = "20260608_000000_rename_guild_slug"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("tasks", "finished_at")


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column("tasks", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
