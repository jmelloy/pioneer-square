"""Add guild_invites table for pending GitHub-username invites.

Revision ID: 20260621_000000_add_guild_invites
Revises: 20260619_000000_rename_claude_usage_to_llm_usage
Create Date: 2026-06-21

Adds ``guild_invites`` to track pending invites addressed to a GitHub
username or numeric ID. When the invited user logs in, ``oauth.create_session``
auto-accepts matching invites and adds them as guild members.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260621_000000_add_guild_invites"
down_revision: str | Sequence[str] | None = "20260619_000000_rename_claude_usage_to_llm_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guild_invites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.Integer(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=True),
        sa.Column("github_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="'member'"),
        sa.Column("invited_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="'pending'"),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.id"]),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_guild_invites_guild_id_status", "guild_invites", ["guild_id", "status"])
    # Partial unique indexes prevent duplicate pending invites at the DB level,
    # closing the TOCTOU race that application-level one_or_none() checks leave open.
    op.create_index(
        "uq_guild_invites_login_pending",
        "guild_invites",
        ["guild_id", "github_login"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND github_login IS NOT NULL"),
    )
    op.create_index(
        "uq_guild_invites_github_id_pending",
        "guild_invites",
        ["guild_id", "github_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND github_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_guild_invites_github_id_pending", table_name="guild_invites")
    op.drop_index("uq_guild_invites_login_pending", table_name="guild_invites")
    op.drop_index("ix_guild_invites_guild_id_status", table_name="guild_invites")
    op.drop_table("guild_invites")
