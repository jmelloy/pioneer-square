"""Add (conversation_id, id) index to foreman_turns.

Revision ID: 20260904_000001_add_foreman_turns_conversation_id_index
Revises: 20260904_000000_add_conversation_id_columns
Create Date: 2026-09-04

Issue #1279 ("update ForemanTurn to use Conversation context") switches
``foreman.history.ConversationHistory._windowed_turns`` to filter on
``foreman_turns.conversation_id`` (OR-falling-back to ``(guild_id,
user_id)`` for unstamped legacy rows) instead of ``(guild_id, user_id)``
alone, and orders by ``id DESC``. The existing
``ix_foreman_turns_guild_id_user_id_id`` composite index doesn't cover that
query shape; this adds the ``conversation_id``-led equivalent so it stays a
backward index scan instead of a sort.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260904_000001_add_foreman_turns_conversation_id_index"
down_revision: str | Sequence[str] | None = "20260904_000000_add_conversation_id_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_foreman_turns_conversation_id_id", "foreman_turns", ["conversation_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_foreman_turns_conversation_id_id", table_name="foreman_turns")
