"""Add excluded_env_keys to workers.

Revision ID: 20260801_000000_add_excluded_env_keys_to_workers
Revises: 20260729_000001_drop_foreman_turns_usage_columns
Create Date: 2026-08-01

The spawn form lets an operator opt individual guild credentials out of a single
launch. Withholding them from the container env was not enough: the worker
re-fetches guild + user env from /guilds/{id}/foreman/env-vars at startup and
applies anything not already in its environment, which handed the excluded keys
straight back. Recording them on the worker row lets that endpoint filter them
for the worker they were excluded for. Nullable, no backfill: existing workers
excluded nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_000000_add_excluded_env_keys_to_workers"
down_revision: str | Sequence[str] | None = "20260729_000001_drop_foreman_turns_usage_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("excluded_env_keys", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "excluded_env_keys")
