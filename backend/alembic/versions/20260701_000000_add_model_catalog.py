"""Add model_catalog table for persisting the models.dev provider/model catalog.

Revision ID: 20260701_000000_add_model_catalog
Revises: 20260623_000000_add_task_id_to_messages
Create Date: 2026-07-01

Stores one row per (provider, model_id) pair fetched from models.dev so the
catalog survives backend restarts and can be consumed at dispatch time without
a live network call.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260701_000000_add_model_catalog"
down_revision: str | Sequence[str] | None = "20260623_000000_add_task_id_to_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_catalog",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider", "model_id"),
    )


def downgrade() -> None:
    op.drop_table("model_catalog")
