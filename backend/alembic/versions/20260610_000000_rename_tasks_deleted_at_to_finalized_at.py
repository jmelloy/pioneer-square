"""Drop tasks.deleted_at; introduce finalized_at for task soft-delete expiry.

``finalized_at`` may already exist on databases where the column was
pre-applied outside the migration chain.  This migration is safe for both
cases:

  1. Adds ``finalized_at`` with ``IF NOT EXISTS`` — a no-op on databases
     that already have the column.
  2. Copies any non-NULL ``deleted_at`` values into ``finalized_at`` where
     ``finalized_at`` is still NULL, preserving existing soft-delete state.
  3. Drops ``deleted_at`` (the now-superseded column).
  4. Creates the composite index on ``finalized_at`` with ``IF NOT EXISTS``.

Revision ID: 20260610_000000_rename_tasks_deleted_at_to_finalized_at
Revises: 20260608_000000_rename_guild_slug
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260610_000000_rename_tasks_deleted_at_to_finalized_at"
down_revision: str | Sequence[str] | None = "20260608_000000_rename_guild_slug"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add finalized_at only if it doesn't already exist (idempotent for DBs
    # where the column was pre-applied outside the migration chain).
    op.execute(
        sa.text(
            "ALTER TABLE tasks"
            " ADD COLUMN IF NOT EXISTS finalized_at TIMESTAMPTZ"
        )
    )

    # Copy any non-NULL deleted_at values into finalized_at where finalized_at
    # is still NULL.  This preserves existing soft-delete state on fresh DBs
    # while leaving pre-existing finalized_at values untouched.
    op.execute(
        sa.text(
            "UPDATE tasks"
            " SET finalized_at = deleted_at"
            " WHERE deleted_at IS NOT NULL AND finalized_at IS NULL"
        )
    )

    # Drop the old index before dropping the column (index may not exist on
    # databases that never had it, so use DROP INDEX IF EXISTS).
    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS ix_tasks_guild_id_deleted_at_created_at"
        )
    )

    # Drop the superseded column.
    op.execute(sa.text("ALTER TABLE tasks DROP COLUMN IF EXISTS deleted_at"))

    # Create the replacement index (IF NOT EXISTS handles DBs that already
    # had this index applied separately).
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_guild_id_finalized_at_created_at"
            " ON tasks (guild_id, finalized_at, created_at)"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP INDEX IF EXISTS ix_tasks_guild_id_finalized_at_created_at"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE tasks"
            " ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tasks"
            " SET deleted_at = finalized_at"
            " WHERE finalized_at IS NOT NULL AND deleted_at IS NULL"
        )
    )
    op.execute(sa.text("ALTER TABLE tasks DROP COLUMN IF EXISTS finalized_at"))
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_tasks_guild_id_deleted_at_created_at"
            " ON tasks (guild_id, deleted_at, created_at)"
        )
    )
