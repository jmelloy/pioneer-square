"""Add bedrock_model_id column to model_catalog.

Revision ID: 20260701_000002_add_bedrock_model_id_to_catalog
Revises: 20260701_000001_add_provider_tool_to_workers
Create Date: 2026-07-01

Stores the canonical AWS Bedrock model ID or cross-region inference profile
ARN (e.g. ``us.anthropic.claude-sonnet-4-6-20251001-v1:0``) alongside the
short models.dev model_id.  NULL until the Bedrock enricher successfully
resolves the ID (requires AWS credentials at catalog refresh time).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260701_000002_add_bedrock_model_id_to_catalog"
down_revision: str | Sequence[str] | None = "20260701_000001_add_provider_tool_to_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_catalog",
        sa.Column("bedrock_model_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_catalog", "bedrock_model_id")
