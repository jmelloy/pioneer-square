"""Add api_calls_json column to foreman_turns.

Revision ID: 20260605_000001_add_api_calls_to_foreman_turns
Revises: 20260605_000000_add_database_indexes
Create Date: 2026-06-05

Stores Anthropic API call metadata (request IDs, model, token counts, timestamps)
captured from each messages.create() call. Attached to the assistant turn so
failures can be correlated with Anthropic-side logs via the request-id header.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605_000001_add_api_calls_to_foreman_turns"
down_revision: str | Sequence[str] | None = "20260605_000000_add_database_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "foreman_turns",
        sa.Column("api_calls_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("foreman_turns", "api_calls_json")
