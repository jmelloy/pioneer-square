"""Index conversation message history in its display order.

The old conversation_id-only index fetched every matching message then sorted
before applying LIMIT 100. Build its replacement concurrently before dropping
it so chat writes and existing lookups can continue during deployment.
"""

from alembic import op

revision = "20261028_000000"
down_revision = "20261026_000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_conversation_id_created_at_id",
            "messages",
            ["conversation_id", "created_at", "id"],
            postgresql_concurrently=True,
        )
        op.drop_index("ix_messages_conversation_id", table_name="messages", postgresql_concurrently=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_conversation_id",
            "messages",
            ["conversation_id"],
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_messages_conversation_id_created_at_id",
            table_name="messages",
            postgresql_concurrently=True,
        )
