"""Add conversations and threads tables; add tasks.thread_id.

Revision ID: 20260813_000001_add_threads_and_conversations
Revises: 20260813_000000_add_soft_delete_guild_members_discord
Create Date: 2026-08-13

Backend half of the thread-per-conversation architecture (epic #1160, issue
#1163). ``conversations`` anchors one interactive Foreman conversation per
(guild, user); ``threads`` binds a conversation to a Discord thread with an
explicit lifecycle (active/archived/closed) that the periodic foreman sweep
(``foreman/thread_maintenance.py``) advances over time. ``tasks.thread_id``
lets a task created from within a conversation route its Discord
notifications into that conversation's thread instead of a separate
task-stream thread.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_000001_add_threads_and_conversations"
down_revision: str | Sequence[str] | None = "20260813_000000_add_soft_delete_guild_members_discord"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.Integer(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversations_guild_id_user_id", "conversations", ["guild_id", "user_id"]
    )

    op.create_table(
        "threads",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("conversations.id"),
            nullable=False,
        ),
        sa.Column("discord_thread_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_threads_conversation_id", "threads", ["conversation_id"])
    op.create_index("ix_threads_status", "threads", ["status"])
    op.create_index(
        "uq_threads_discord_thread_id_active",
        "threads",
        ["discord_thread_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND discord_thread_id IS NOT NULL"),
    )

    op.add_column("tasks", sa.Column("thread_id", sa.Text(), nullable=True))
    op.create_index("ix_tasks_thread_id", "tasks", ["thread_id"])
    op.create_foreign_key(
        "fk_tasks_thread_id_threads", "tasks", "threads", ["thread_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_tasks_thread_id_threads", "tasks", type_="foreignkey")
    op.drop_index("ix_tasks_thread_id", table_name="tasks")
    op.drop_column("tasks", "thread_id")

    op.drop_index("uq_threads_discord_thread_id_active", table_name="threads")
    op.drop_index("ix_threads_status", table_name="threads")
    op.drop_index("ix_threads_conversation_id", table_name="threads")
    op.drop_table("threads")

    op.drop_index("ix_conversations_guild_id_user_id", table_name="conversations")
    op.drop_table("conversations")
