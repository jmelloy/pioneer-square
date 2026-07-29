"""Add guild_id and user_id to api_request_log.

Revision ID: 20260728_000001_add_guild_user_to_api_request_log
Revises: 20260728_000000_add_last_refreshed_at_to_github_cache
Create Date: 2026-07-28

Adds the guild (int FK to guilds.id) and github user id an API call was made
on behalf of, so usage/cost can be attributed per-guild and per-user. Both
nullable: pre-existing rows stay NULL, as do system/worker-driven calls.

Note: request_id (the Anthropic x-request-id HTTP header, e.g. req_...) and
response->>'id' (the Message object id, e.g. msg_...) are DIFFERENT ids, so
there is no backfill of request_id from the response body.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_000001_add_guild_user_to_api_request_log"
down_revision: str | Sequence[str] | None = "20260728_000000_add_last_refreshed_at_to_github_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_request_log", sa.Column("guild_id", sa.Integer(), nullable=True))
    op.add_column("api_request_log", sa.Column("user_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_api_request_log_guild_id",
        "api_request_log",
        "guilds",
        ["guild_id"],
        ["id"],
    )
    op.create_index("ix_api_request_log_guild_id", "api_request_log", ["guild_id"])


def downgrade() -> None:
    op.drop_index("ix_api_request_log_guild_id", table_name="api_request_log")
    op.drop_constraint("fk_api_request_log_guild_id", "api_request_log", type_="foreignkey")
    op.drop_column("api_request_log", "user_id")
    op.drop_column("api_request_log", "guild_id")
