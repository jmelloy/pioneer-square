"""Add discord_foreman_threads table for per-session Foreman chat threads.

Revision ID: 20260704_000002_add_discord_foreman_threads
Revises: 20260704_000001_add_discord_users
Create Date: 2026-07-04

Maps a (guild_id, session_key) pair — one calendar day of Foreman chat, per
guild — to a Discord thread channel ID so Foreman<->user narration can be
mirrored into the notification channel without re-creating the thread on
every turn or every restart.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260704_000002_add_discord_foreman_threads"
down_revision: str | Sequence[str] | None = "20260704_000001_add_discord_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_foreman_threads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_discord_foreman_threads_guild_session",
        "discord_foreman_threads",
        ["guild_id", "session_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_discord_foreman_threads_guild_session", table_name="discord_foreman_threads"
    )
    op.drop_table("discord_foreman_threads")
