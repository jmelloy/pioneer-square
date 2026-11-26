"""Index conversation keyword search with PostgreSQL FTS (#1531)."""

import sqlalchemy as sa
from alembic import op

revision = "20261125_000000"
down_revision = "20261124_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_conversation_content_fts",
            "messages",
            [sa.text("to_tsvector('english'::regconfig, content)")],
            postgresql_using="gin",
            postgresql_where=sa.text("conversation_id IS NOT NULL"),
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_conversations_name_fts",
            "conversations",
            [sa.text("to_tsvector('english'::regconfig, coalesce(name, ''))")],
            postgresql_using="gin",
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_conversations_name_fts",
            table_name="conversations",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_messages_conversation_content_fts",
            table_name="messages",
            postgresql_concurrently=True,
        )
