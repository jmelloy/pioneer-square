"""Drop deprecated usage columns from foreman_turns.

Revision ID: 20260729_000001_drop_foreman_turns_usage_columns
Revises: 20260729_000000_add_trigger_to_api_request_log
Create Date: 2026-07-29

Removes three columns superseded by api_request_log:
  - input_tokens / output_tokens: usage now lives on api_request_log, linked
    via foreman_turns.request_id (FK). These were no longer written.
  - api_calls_json: per-call metadata now fully captured by the api_request_log
    row the turn's request_id points at.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260729_000001_drop_foreman_turns_usage_columns"
down_revision: str | Sequence[str] | None = "20260729_000000_add_trigger_to_api_request_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("foreman_turns", "api_calls_json")
    op.drop_column("foreman_turns", "output_tokens")
    op.drop_column("foreman_turns", "input_tokens")


def downgrade() -> None:
    op.add_column("foreman_turns", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("foreman_turns", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column("foreman_turns", sa.Column("api_calls_json", sa.Text(), nullable=True))
