"""Add thread_id to ForemanTurn for conversation-scoped history.

Revision ID: 20260904_000000_add_thread_id_to_foreman_turns
Revises: 20260831_000000_finish_spawn_settings_migration
Create Date: 2026-09-04

Part of #1271: makes Conversation (via Thread) the core foreman thread model.
Each ForemanTurn is now directly linked to the Thread it belongs to, enabling
conversation-scoped history retrieval without depending on (guild_id, user_id)
pair lookups throughout the codebase.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_000000_add_thread_id_to_foreman_turns"
down_revision: str | Sequence[str] | None = "20260831_000000_finish_spawn_settings_migration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add thread_id column to foreman_turns
    op.add_column("foreman_turns", sa.Column("thread_id", sa.Text(), nullable=True))
    op.create_index("ix_foreman_turns_thread_id", "foreman_turns", ["thread_id"])
    op.create_foreign_key(
        "fk_foreman_turns_thread_id_threads",
        "foreman_turns",
        "threads",
        ["thread_id"],
        ["id"],
    )

    # Note: We do NOT populate thread_id from existing rows here because
    # the conversation->thread relationship requires resolving the Conversation
    # for each turn's (guild_id, user_id) pair, which is better done via a
    # one-off script. New turns after this migration will have thread_id set.


def downgrade() -> None:
    op.drop_constraint("fk_foreman_turns_thread_id_threads", "foreman_turns", type_="foreignkey")
    op.drop_index("ix_foreman_turns_thread_id", table_name="foreman_turns")
    op.drop_column("foreman_turns", "thread_id")
