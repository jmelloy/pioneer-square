"""add spawn_settings tool_defaults

Revision ID: 20260821_000000
Revises: 20260815_000000_add_thread_id_to_messages
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260821_000000"
down_revision = "20260815_000000_add_thread_id_to_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "spawn_settings",
        sa.Column("tool_defaults", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("spawn_settings", "tool_defaults")
