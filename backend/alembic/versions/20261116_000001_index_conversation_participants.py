"""Cover plain-chat participant summaries without reading message content."""

import sqlalchemy as sa
from alembic import op

revision = "20261116_000001"
down_revision = "20261116_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_conversation_plain_senders",
            "messages",
            ["conversation_id", "from_agent", "user_id", "created_at", "id"],
            postgresql_where=sa.text("role IS NULL"),
            postgresql_include=["guild_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_messages_conversation_plain_senders",
            table_name="messages",
            postgresql_concurrently=True,
        )
