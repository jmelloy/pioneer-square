"""Add available_models to workers.

Revision ID: 20260814_000000_add_available_models_to_workers
Revises: 20260813_000001_add_threads_and_conversations
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_000000_add_available_models_to_workers"
down_revision: str | Sequence[str] | None = "20260813_000001_add_threads_and_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("available_models", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "available_models")
