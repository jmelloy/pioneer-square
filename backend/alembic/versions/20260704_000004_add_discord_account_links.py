"""Add discord_connect_tokens and discord_account_links tables.

Revision ID: 20260704_000004_add_discord_account_links
Revises: 20260704_000003_add_discord_channel_guilds
Create Date: 2026-07-04

Backs the ``/connect-account`` slash command: a one-time token is minted
into ``discord_connect_tokens`` and redeemed via ``POST /api/discord/connect``
once the user is logged in, creating/updating a row in
``discord_account_links``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_000004_add_discord_account_links"
down_revision: str | Sequence[str] | None = "20260704_000003_add_discord_channel_guilds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_connect_tokens",
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("discord_user_id", sa.Text(), nullable=False),
        sa.Column("discord_username", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token"),
    )

    op.create_table(
        "discord_account_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ps_user_id", sa.Text(), nullable=False),
        sa.Column("discord_user_id", sa.Text(), nullable=False),
        sa.Column("discord_username", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ps_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discord_user_id", name="uq_discord_account_links_discord_user_id"),
    )


def downgrade() -> None:
    op.drop_table("discord_account_links")
    op.drop_table("discord_connect_tokens")
