"""Add discord_users table mapping GitHub logins to Discord user IDs.

Revision ID: 20260704_000001_add_discord_users
Revises: 20260704_000000_merge_discord_threads_and_model_tier
Create Date: 2026-07-04

Lets the notifier turn a plain ``@login`` reference into a real Discord
``<@id>`` mention when a human has linked the two accounts via the
``/api/discord-users`` REST endpoints.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_000001_add_discord_users"
down_revision: str | Sequence[str] | None = "20260704_000000_merge_discord_threads_and_model_tier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_users",
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("discord_user_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("github_login"),
    )


def downgrade() -> None:
    op.drop_table("discord_users")
