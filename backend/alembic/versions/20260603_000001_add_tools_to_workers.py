"""Add tools column to workers table.

Revision ID: 20260603_000001_add_tools_to_workers
Revises: 20260603_000000_create_user_spawn_settings
Create Date: 2026-06-03

Stores the JSON-serialised list of available tool runners (e.g. ["claude", "codex", "pi"])
that the worker reported at registration time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260603_000001_add_tools_to_workers"
down_revision: str | Sequence[str] | None = "20260601_000000_add_model_provider_to_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column("tools", sa.Text(), nullable=False, server_default="'[]'"),
    )


def downgrade() -> None:
    op.drop_column("workers", "tools")
