"""Add spawn_profiles table.

Revision ID: 20260803_000000_add_spawn_profiles
Revises: 20260801_000000_add_hostname_to_workers
Create Date: 2026-08-03

A named, reusable spawn-worker configuration ("predefined worker config") a
guild or user can select by name at spawn time, in addition to the always-on
spawn_settings baseline/override. See models.SpawnProfile and
spawn_profiles.py for the built-in profile set and resolution order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_000000_add_spawn_profiles"
down_revision: str | Sequence[str] | None = "20260801_000000_add_hostname_to_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spawn_profiles",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tool", sa.Text(), nullable=False, server_default=sa.text("'claude'")),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("credentials_source", sa.Text(), nullable=True),
        sa.Column("default_agent_count", sa.Integer(), nullable=True),
        sa.Column("default_repos", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
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


def downgrade() -> None:
    op.drop_index("uq_spawn_profiles_guild_user_name", table_name="spawn_profiles")
    op.drop_index("uq_spawn_profiles_guild_name", table_name="spawn_profiles")
    op.drop_table("spawn_profiles")
