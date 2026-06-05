"""Refactor foreman_turns: add request_id FK and task_id columns.

Revision ID: 20260605_000003_refactor_foreman_turns
Revises: 20260605_000002_add_api_request_log
Create Date: 2026-06-05

Adds:
  - foreman_turns.request_id  INT  FK → api_request_log.id  (nullable)
  - foreman_turns.task_id     TEXT FK → tasks.id            (nullable)

Usage columns (input_tokens, output_tokens) are left as-is — nullable and
kept for historical rows. New rows should get usage from api_request_log
instead; foreman runner no longer writes those columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605_000003_refactor_foreman_turns"
down_revision: str | Sequence[str] | None = "20260605_000002_add_api_request_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "foreman_turns",
        sa.Column(
            "request_id",
            sa.Integer(),
            sa.ForeignKey("api_request_log.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "foreman_turns",
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("tasks.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_foreman_turns_request_id", "foreman_turns", ["request_id"])
    op.create_index("ix_foreman_turns_task_id", "foreman_turns", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_foreman_turns_task_id", table_name="foreman_turns")
    op.drop_index("ix_foreman_turns_request_id", table_name="foreman_turns")
    op.drop_column("foreman_turns", "task_id")
    op.drop_column("foreman_turns", "request_id")
