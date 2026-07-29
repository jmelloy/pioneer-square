"""Add trigger to api_request_log.

Revision ID: 20260729_000000_add_trigger_to_api_request_log
Revises: 20260728_000002_add_spawn_settings
Create Date: 2026-07-29

Adds the trigger source of the foreman run each API call belongs to (the
_trigger_foreman event vocabulary plus github-event / user-followup), so token
usage can be attributed per trigger — periodic-check vs github vs chat vs
task-lifecycle — instead of inferring it from message markers. Nullable:
pre-existing rows stay NULL (no backfill). Indexed for GROUP BY trigger.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260729_000000_add_trigger_to_api_request_log"
down_revision: str | Sequence[str] | None = "20260728_000002_add_spawn_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_request_log", sa.Column("trigger", sa.Text(), nullable=True))
    op.create_index("ix_api_request_log_trigger", "api_request_log", ["trigger"])


def downgrade() -> None:
    op.drop_index("ix_api_request_log_trigger", table_name="api_request_log")
    op.drop_column("api_request_log", "trigger")
