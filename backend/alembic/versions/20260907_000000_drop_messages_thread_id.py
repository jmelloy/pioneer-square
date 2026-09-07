"""Drop messages.thread_id now that Message.conversation_id is the sole reference.

Revision ID: 20260907_000000_drop_messages_thread_id
Revises: 20260904_020000_add_ui_lifecycle_fields_to_conversations
Create Date: 2026-09-07

Issue #1290 (epic #1271, "make Conversation the core Foreman thread model"):
``messages.conversation_id`` (added + backfilled by
``20260904_000000_add_conversation_id_columns``) has been the column every
read site scopes message history by since #1275 (``routes/threads.py``'s
thread-messages endpoint already joins through ``Conversation``, not
``Message.thread_id``). The three remaining write sites
(``ws_handlers.py``, ``discord/router.py``, ``foreman/journal.py``) already
dual-wrote both columns from independently-resolved values, so this drops
the now-redundant ``thread_id`` column and its index outright rather than
carrying it forward as dead weight.

Downgrade re-adds the column empty (nullable, unbackfilled) — matching every
other column-drop migration in this history — since the only backfill
source (``threads.conversation_id`` join) has already been superseded by
direct ``conversation_id`` writes and can't reconstruct which specific
``Thread`` a message was posted in after the fact.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_000000_drop_messages_thread_id"
down_revision: str | Sequence[str] | None = "20260904_020000_add_ui_lifecycle_fields_to_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_messages_thread_id", table_name="messages")
    op.drop_column("messages", "thread_id")


def downgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("thread_id", sa.String(), sa.ForeignKey("threads.id"), nullable=True),
    )
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"])
