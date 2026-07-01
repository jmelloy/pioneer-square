"""Add provider and tool columns to workers table.

Revision ID: 20260701_000001_add_provider_tool_to_workers
Revises: 20260701_000000_add_model_catalog
Create Date: 2026-07-01

provider: the AI provider this worker communicates with (e.g. 'anthropic', 'bedrock', 'openai').
tool: the primary tool runner this worker is configured for (e.g. 'claude', 'pi', 'codex').
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260701_000001_add_provider_tool_to_workers"
down_revision: str | Sequence[str] | None = "20260701_000000_add_model_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column("provider", sa.Text(), nullable=True),
    )
    op.add_column(
        "workers",
        sa.Column("tool", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workers", "tool")
    op.drop_column("workers", "provider")
