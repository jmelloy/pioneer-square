"""Add worker lifecycle tracking columns to workers table.

Revision ID: 20260607_000000_add_worker_lifecycle_columns
Revises: 20260606_000000_index_tasks_parent_task_id
Create Date: 2026-06-07

Adds four nullable columns to ``workers`` that enable stale-worker detection
and graceful restart when the backend version changes:

  container_id       — full Docker container id; used to force-kill stale containers
  spawned_version    — backend version at spawn time (PIONEER_VERSION or git SHA)
  started_at         — UTC instant the container was confirmed running
  drain_requested_at — UTC instant a graceful-shutdown signal was sent
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260607_000000_add_worker_lifecycle_columns"
down_revision: str | Sequence[str] | None = "20260606_000000_index_tasks_parent_task_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("container_id", sa.Text(), nullable=True))
    op.add_column("workers", sa.Column("spawned_version", sa.Text(), nullable=True))
    op.add_column("workers", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "workers", sa.Column("drain_requested_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("workers", "drain_requested_at")
    op.drop_column("workers", "started_at")
    op.drop_column("workers", "spawned_version")
    op.drop_column("workers", "container_id")
