"""Add soft delete columns to guild_members, discord_channel_guilds, discord_account_links.

Revision ID: 20260813_000000_add_soft_delete_guild_members_discord
Revises: 20260812_000002
Create Date: 2026-08-13

Part of the soft-delete rollout (issue #1166): these three tables gained a
``deleted_at`` column and ``SoftDeleteMixin`` on their model classes, but the
plain unique constraints/indexes backing their natural keys were never
replaced with partial unique indexes scoped to ``deleted_at IS NULL``. Without
that swap, soft-deleting a row (e.g. leaving a guild, unlinking a Discord
account) would permanently block reusing that key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_000000_add_soft_delete_guild_members_discord"
down_revision: str | Sequence[str] | None = "20260812_000002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # guild_members
    op.add_column(
        "guild_members", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_index("uq_guild_members_guild_id_user_id", table_name="guild_members")
    op.create_index(
        "uq_guild_members_guild_id_user_id_active",
        "guild_members",
        ["guild_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # discord_channel_guilds
    op.add_column(
        "discord_channel_guilds",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_index(
        "uq_discord_channel_guilds_guild_channel", table_name="discord_channel_guilds"
    )
    op.create_index(
        "uq_discord_channel_guilds_guild_channel_active",
        "discord_channel_guilds",
        ["discord_guild_id", "discord_channel_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # discord_account_links
    op.add_column(
        "discord_account_links",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "uq_discord_account_links_discord_user_id",
        "discord_account_links",
        type_="unique",
    )
    op.create_index(
        "uq_discord_account_links_discord_user_id_active",
        "discord_account_links",
        ["discord_user_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_discord_account_links_discord_user_id_active",
        table_name="discord_account_links",
    )
    op.create_unique_constraint(
        "uq_discord_account_links_discord_user_id",
        "discord_account_links",
        ["discord_user_id"],
    )
    op.drop_column("discord_account_links", "deleted_at")

    op.drop_index(
        "uq_discord_channel_guilds_guild_channel_active",
        table_name="discord_channel_guilds",
    )
    op.create_index(
        "uq_discord_channel_guilds_guild_channel",
        "discord_channel_guilds",
        ["discord_guild_id", "discord_channel_id"],
        unique=True,
    )
    op.drop_column("discord_channel_guilds", "deleted_at")

    op.drop_index("uq_guild_members_guild_id_user_id_active", table_name="guild_members")
    op.create_index(
        "uq_guild_members_guild_id_user_id",
        "guild_members",
        ["guild_id", "user_id"],
        unique=True,
    )
    op.drop_column("guild_members", "deleted_at")
