"""Add model and provider columns to tasks.

Revision ID: 20260601_000000_add_model_provider_to_tasks
Revises: 20260530_000000_add_level_to_task_logs
Create Date: 2026-06-01

Adds nullable ``model`` and ``provider`` columns so per-task AI model
and provider overrides can be stored alongside the tool selection.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260601_000000_add_model_provider_to_tasks"
down_revision: str | Sequence[str] | None = "20260530_000000_add_level_to_task_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("model", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("provider", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "provider")
    op.drop_column("tasks", "model")
