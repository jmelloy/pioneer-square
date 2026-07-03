"""Add model_tier column to tasks table.

Revision ID: 20260703_000000_add_model_tier_to_tasks
Revises: 20260701_000003_add_issue_state_to_tasks
Create Date: 2026-07-03

Records the capability tier (cheap/standard/powerful) at which each task was
dispatched. Derived from phase+tool at assign_task time. Historical rows remain
NULL without errors.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260703_000000_add_model_tier_to_tasks"
down_revision: str | Sequence[str] | None = "20260701_000003_add_issue_state_to_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("model_tier", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "model_tier")
