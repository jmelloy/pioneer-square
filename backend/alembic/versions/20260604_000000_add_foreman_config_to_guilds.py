"""Add foreman_config column to guilds table.

Revision ID: 20260604_000000_add_foreman_config_to_guilds
Revises: 20260603_000001_add_tools_to_workers
Create Date: 2026-06-04

Stores per-guild foreman AI configuration (model, provider, system prompt suffix,
poll intervals, max rounds) as a JSON text column. NULL = use process-level env-var
defaults, so existing deployments are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260604_000000_add_foreman_config_to_guilds"
down_revision: str | Sequence[str] | None = "20260603_000001_add_tools_to_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "guilds",
        sa.Column("foreman_config", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("guilds", "foreman_config")
