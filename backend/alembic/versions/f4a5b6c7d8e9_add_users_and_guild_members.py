"""add_users_and_guild_members

Adds a canonical ``users`` table and a ``guild_members`` table with a per-guild
role (owner/member/viewer). Also adds ``workers.user_id`` so a worker process
can attribute its actions to a specific human.

Backfill notes:
- Every existing ``github_tokens`` row becomes a ``users`` row (id == github_user_id).
- Every existing guild with ``github_user_id`` set gets an ``owner`` membership.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-05-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("github_id", sa.Text(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("github_id"),
    )

    op.create_table(
        "guild_members",
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="member", nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("guild_id", "user_id"),
    )

    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Text(), nullable=True))

    # Backfill users from github_tokens (one row per known GitHub user).
    op.execute(
        """
        INSERT INTO users (id, github_id, github_login, created_at, updated_at)
        SELECT github_user_id, github_user_id, github_username, created_at, updated_at
        FROM github_tokens
        WHERE github_user_id IS NOT NULL
        """
    )

    # Backfill owner membership for guilds that already have a linked user.
    op.execute(
        """
        INSERT INTO guild_members (guild_id, user_id, role, created_at)
        SELECT g.id, g.github_user_id, 'owner', g.created_at
        FROM guilds g
        WHERE g.github_user_id IS NOT NULL
          AND g.github_user_id IN (SELECT id FROM users)
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("user_id")
    op.drop_table("guild_members")
    op.drop_table("users")
