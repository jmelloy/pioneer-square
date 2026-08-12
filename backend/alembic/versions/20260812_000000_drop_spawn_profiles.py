"""Drop unused spawn_profiles table.

Revision ID: 20260812_000000_drop_spawn_profiles
Revises: 20260811_000000_default_tool_pi
Create Date: 2026-08-12 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_000000_drop_spawn_profiles"
down_revision: str | Sequence[str] | None = "20260811_000000_default_tool_pi"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_spawn_profiles_guild_user_name", table_name="spawn_profiles")
    op.drop_index("uq_spawn_profiles_guild_name", table_name="spawn_profiles")
    op.drop_table("spawn_profiles")


def downgrade() -> None:
    op.create_table(
        "spawn_profiles",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("guild_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tool", sa.Text(), server_default=sa.text("'pi'"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("credentials_source", sa.Text(), nullable=True),
        sa.Column("default_agent_count", sa.Integer(), nullable=True),
        sa.Column("default_repos", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_spawn_profiles_guild_name",
        "spawn_profiles",
        ["guild_id", "name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "uq_spawn_profiles_guild_user_name",
        "spawn_profiles",
        ["guild_id", "user_id", "name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
