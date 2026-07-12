"""Add claude_session_id column to tasks.

Revision ID: 20260712_000000_add_claude_session_id_to_tasks
Revises: 20260711_000001_unify_discord_thread_bindings
Create Date: 2026-07-12

Part of cross-dispatch agent session resumption (#896). Stores the session
ID the coding agent (Claude/codex/pi) reports at task completion so a later
``send_followup`` dispatched back to the same worker can resume the prior
conversation instead of starting fresh. NULL on legacy tasks and on any task
whose agent doesn't report a session ID.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260712_000000_add_claude_session_id_to_tasks"
down_revision: str | Sequence[str] | None = "20260711_000001_unify_discord_thread_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("claude_session_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "claude_session_id")
