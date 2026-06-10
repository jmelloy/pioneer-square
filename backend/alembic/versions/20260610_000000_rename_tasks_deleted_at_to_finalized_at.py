"""Rename tasks.deleted_at to tasks.finalized_at.

Revision ID: 20260610_000000_rename_tasks_deleted_at_to_finalized_at
Revises: 20260608_000000_rename_guild_slug
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260610_000000_rename_tasks_deleted_at_to_finalized_at"
down_revision: str | Sequence[str] | None = "20260608_000000_rename_guild_slug"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_tasks_guild_id_deleted_at_created_at", table_name="tasks")
    op.alter_column("tasks", "deleted_at", new_column_name="finalized_at")
    op.create_index(
        "ix_tasks_guild_id_finalized_at_created_at", "tasks", ["guild_id", "finalized_at", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_guild_id_finalized_at_created_at", table_name="tasks")
    op.alter_column("tasks", "finalized_at", new_column_name="deleted_at")
    op.create_index(
        "ix_tasks_guild_id_deleted_at_created_at", "tasks", ["guild_id", "deleted_at", "created_at"]
    )
