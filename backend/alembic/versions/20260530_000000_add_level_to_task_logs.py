"""Add level column to task_logs.

Revision ID: 20260530_000000_add_level_to_task_logs
Revises: 20260530_000000_create_claude_usage
Create Date: 2026-05-30

Adds a nullable ``level`` column that types each log line semantically
(worker/auth/claude/thinking/…) so the frontend can style logs without
parsing text prefixes like ``[worker]``. NULL = default agent output.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260530_000000_add_level_to_task_logs"
down_revision: str | Sequence[str] | None = "20260530_000000_create_claude_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_logs", sa.Column("level", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_logs", "level")
