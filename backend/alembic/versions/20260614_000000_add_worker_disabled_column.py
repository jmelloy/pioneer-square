"""Add disabled column to workers table.

Revision ID: 20260614_000000_add_worker_disabled_column
Revises: 20260610_000000_drop_tasks_finished_at
Create Date: 2026-06-14

Adds a ``disabled`` boolean column to ``workers``.  When the foreman explicitly
shuts down a worker via the ``shutdown_worker`` tool the column is set to True,
preventing the worker from being re-spawned on the next backend restart.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260614_000000_add_worker_disabled_column"
down_revision: str | Sequence[str] | None = "20260610_000000_drop_tasks_finished_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("workers", "disabled")
