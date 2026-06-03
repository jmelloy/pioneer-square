"""Create user_spawn_settings table.

Revision ID: 20260603_000000_create_user_spawn_settings
Revises: 20260530_000000_add_level_to_task_logs
Create Date: 2026-06-03

Stores per-user, per-guild spawn-worker preferences (repos, tools, env var
key-value pairs) in the database so that env var values are not exposed in
browser localStorage.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260603_000000_create_user_spawn_settings"
down_revision: str | Sequence[str] | None = "20260530_000000_add_level_to_task_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_spawn_settings",
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id"), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_spawn_settings")
