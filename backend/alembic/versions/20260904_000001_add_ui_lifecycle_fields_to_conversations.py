"""Move Thread UI/lifecycle fields (name, status, discord_thread_id) into Conversation.

Revision ID: 20260904_000001_add_ui_lifecycle_fields_to_conversations
Revises: 20260831_000000
Create Date: 2026-09-04

Issue #1274 (epic #1271, "make Conversation the core Foreman thread model"):
adds ``name``/``status``/``discord_thread_id`` to ``conversations``, mirroring
the conversation's currently active :class:`Thread` (kept in sync going
forward by ``foreman.thread_service``). Backfills existing conversations from
each one's most-recently-updated, non-deleted thread. ``threads`` keeps its
own copies of these columns — a conversation can have many threads over its
lifetime (see ``models.Thread``'s docstring) — so this is additive, not a
column move at the schema level.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_000001_add_ui_lifecycle_fields_to_conversations"
down_revision: str | Sequence[str] | None = "20260831_000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("name", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
    )
    op.add_column("conversations", sa.Column("discord_thread_id", sa.Text(), nullable=True))

    op.create_index("ix_conversations_status", "conversations", ["status"])
    op.create_index(
        "uq_conversations_discord_thread_id_active",
        "conversations",
        ["discord_thread_id"],
        unique=True,
        postgresql_where=sa.text("discord_thread_id IS NOT NULL"),
    )

    # Backfill from each conversation's most-recently-updated, non-deleted
    # thread — the one ``thread_service.get_or_create_active_thread`` would
    # currently treat as "the" thread for that conversation.
    op.execute(
        """
        UPDATE conversations AS c
        SET name = t.name,
            status = t.status,
            discord_thread_id = t.discord_thread_id
        FROM (
            SELECT DISTINCT ON (conversation_id)
                conversation_id, name, status, discord_thread_id
            FROM threads
            WHERE deleted_at IS NULL
            ORDER BY conversation_id, updated_at DESC
        ) AS t
        WHERE c.id = t.conversation_id
        """
    )


def downgrade() -> None:
    op.drop_index("uq_conversations_discord_thread_id_active", table_name="conversations")
    op.drop_index("ix_conversations_status", table_name="conversations")
    op.drop_column("conversations", "discord_thread_id")
    op.drop_column("conversations", "status")
    op.drop_column("conversations", "name")
