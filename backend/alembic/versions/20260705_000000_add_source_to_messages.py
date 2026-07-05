"""add_source_to_messages

Tags each chat message with its origin ("web", "discord", "api") so the
frontend can show where a message came from (#753). Existing rows predate
Discord/API routing and were all typed in the browser, so they backfill to
"web" via the server default.

Revision ID: 20260705_000000_add_source_to_messages
Revises: 20260704_000004_add_discord_account_links
Create Date: 2026-07-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260705_000000_add_source_to_messages"
down_revision: str | Sequence[str] | None = "20260704_000004_add_discord_account_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(
            sa.Column("source", sa.Text(), nullable=False, server_default="web"),
        )


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("source")
