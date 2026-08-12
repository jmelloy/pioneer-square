"""Repair api_request_log guild/user attribution columns.

Revision ID: 20260812_000001_repair_api_request_log_guild_user
Revises: 20260812_000000_drop_spawn_profiles
Create Date: 2026-08-12 00:01:00.000000

Some long-lived development databases were stamped past
20260728_000001_add_guild_user_to_api_request_log without the actual columns.
Make the current head self-healing so ApiRequestLog inserts that include
``guild_id``/``user_id`` do not fail.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_000001_repair_api_request_log_guild_user"
down_revision: str | Sequence[str] | None = "20260812_000000_drop_spawn_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns("api_request_log")}


def _indexes() -> set[str]:
    bind = op.get_bind()
    return {i["name"] for i in sa.inspect(bind).get_indexes("api_request_log")}


def _foreign_keys() -> set[str | None]:
    bind = op.get_bind()
    return {fk["name"] for fk in sa.inspect(bind).get_foreign_keys("api_request_log")}


def upgrade() -> None:
    cols = _columns()
    if "guild_id" not in cols:
        op.add_column("api_request_log", sa.Column("guild_id", sa.Integer(), nullable=True))
    if "user_id" not in cols:
        op.add_column("api_request_log", sa.Column("user_id", sa.Text(), nullable=True))

    if "ix_api_request_log_guild_id" not in _indexes():
        op.create_index("ix_api_request_log_guild_id", "api_request_log", ["guild_id"])

    if "fk_api_request_log_guild_id" not in _foreign_keys():
        op.create_foreign_key(
            "fk_api_request_log_guild_id",
            "api_request_log",
            "guilds",
            ["guild_id"],
            ["id"],
        )


def downgrade() -> None:
    # Intentionally no-op. These columns are part of the desired schema since
    # 20260728_000001; this migration only repairs databases that missed them.
    pass
