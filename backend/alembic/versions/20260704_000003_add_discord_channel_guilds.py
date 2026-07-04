"""Add discord_channel_guilds table binding Discord channels to PS guilds.

Revision ID: 20260704_000003_add_discord_channel_guilds
Revises: 20260704_000002_add_discord_foreman_threads
Create Date: 2026-07-04

Backs the ``/join-channel`` and ``/leave-channel`` slash commands, letting an
operator wire a Discord channel to a Pioneer Square guild without editing
env vars or restarting the bot.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_000003_add_discord_channel_guilds"
down_revision: str | Sequence[str] | None = "20260704_000002_add_discord_foreman_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_channel_guilds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_guild_id", sa.Text(), nullable=False),
        sa.Column("discord_channel_id", sa.Text(), nullable=False),
        sa.Column("ps_guild_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_channel_guilds_guild_channel",
        "discord_channel_guilds",
        ["discord_guild_id", "discord_channel_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_discord_channel_guilds_guild_channel", table_name="discord_channel_guilds")
    op.drop_table("discord_channel_guilds")
