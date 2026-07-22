"""add_discord_bot_fields_to_users

Adds ``bot_provider``, ``discord_channel_id``, and ``bot_metadata`` to
``users`` so the ``message_discord_bot`` foreman tool (see
``foreman/tools.py``) can look up a bot's delivery method without a second
table join. ``is_bot`` already exists (see
``20260715_000000_add_bot_fields_to_users``); this migration only adds the
Discord-specific delivery fields. Populated by ``discord/bot_users.py`` at
auto-provisioning time; NULL for human users and for bots not yet re-seen
since this migration.

Revision ID: 20260722_000000_add_discord_bot_fields_to_users
Revises: 20260717_000000_add_external_a2a_requests
Create Date: 2026-07-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_000000_add_discord_bot_fields_to_users"
down_revision: str | Sequence[str] | None = "20260717_000000_add_external_a2a_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("bot_provider", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("discord_channel_id", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("bot_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("bot_metadata")
        batch_op.drop_column("discord_channel_id")
        batch_op.drop_column("bot_provider")
