"""add_guild_spawn_defaults

Creates ``guild_spawn_defaults`` — one row per guild holding the default
spawn-worker parameters (repos, tools, agent_count) the foreman uses when a
``spawn_worker`` call doesn't specify them. The row tracks the *last
successful spawn* for the guild and is upserted on every worker spawn.

Data migration: seeds each guild's row from its most recent worker row,
preferring workers that were actually spawned by the backend (``container_id``
is recorded only after a successful container/ECS start) over plain
registrations, newest first. Guilds with no worker carrying a non-empty repos
list get no row — the foreman then requires explicit repos until the first
spawn records defaults.

Revision ID: 20260723_000000_add_guild_spawn_defaults
Revises: 20260722_000000_add_discord_bot_fields_to_users
Create Date: 2026-07-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260723_000000_add_guild_spawn_defaults"
down_revision: str | Sequence[str] | None = "20260722_000000_add_discord_bot_fields_to_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guild_spawn_defaults",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("repos", sa.Text(), nullable=False, server_default="'[]'"),
        sa.Column("tools", sa.Text(), nullable=False, server_default="'[]'"),
        sa.Column("agent_count", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_guild_spawn_defaults_guild_id",
        "guild_spawn_defaults",
        ["guild_id"],
        unique=True,
    )

    # Seed each guild from its most recent worker with a non-empty repos list,
    # preferring backend-spawned workers (container_id recorded on successful
    # start) over plain registrations. Portable across Postgres and SQLite:
    # window functions and CASE ordering work on both.
    op.execute(
        sa.text(
            """
            INSERT INTO guild_spawn_defaults (guild_id, repos, tools, agent_count, updated_at)
            SELECT guild_id, repos, tools, NULL, CURRENT_TIMESTAMP
            FROM (
                SELECT
                    w.guild_id,
                    w.repos,
                    COALESCE(w.tools, '[]') AS tools,
                    ROW_NUMBER() OVER (
                        PARTITION BY w.guild_id
                        ORDER BY
                            CASE WHEN w.container_id IS NOT NULL THEN 0 ELSE 1 END,
                            w.created_at DESC
                    ) AS rn
                FROM workers w
                WHERE w.repos IS NOT NULL AND w.repos != '[]' AND w.repos != ''
            ) latest
            WHERE latest.rn = 1
            """
        )
    )


def downgrade() -> None:
    op.drop_index("uq_guild_spawn_defaults_guild_id", table_name="guild_spawn_defaults")
    op.drop_table("guild_spawn_defaults")
