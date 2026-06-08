"""Rename guilds.guild_id to guilds.slug.

Revision ID: 20260608_000000_rename_guild_slug
Revises: 20260607_000000_add_worker_lifecycle_columns
Create Date: 2026-06-08

The human-readable 6-char guild identifier was stored as ``guild_id`` on the
``guilds`` table; it is renamed to ``slug`` to distinguish it clearly from the
integer primary key and from the integer FK columns named ``guild_id`` on all
child tables (agents, workers, tasks, etc.).

Child-table ``guild_id`` columns are NOT touched — those are integer FKs to
``guilds.id`` and remain unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260608_000000_rename_guild_slug"
down_revision: str | Sequence[str] | None = "20260607_000000_add_worker_lifecycle_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the old partial unique index on guild_id before renaming the column.
    op.drop_index("uq_guilds_guild_id_active", table_name="guilds")

    # Rename the column on the guilds table.
    op.alter_column("guilds", "guild_id", new_column_name="slug")

    # Re-create the partial unique index under the new column name.
    op.create_index(
        "uq_guilds_slug_active",
        "guilds",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_guilds_slug_active", table_name="guilds")
    op.alter_column("guilds", "slug", new_column_name="guild_id")
    op.create_index(
        "uq_guilds_guild_id_active",
        "guilds",
        ["guild_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
