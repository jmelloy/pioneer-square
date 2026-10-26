"""Index PR-event reconciliation and shared conversation-context lookups."""

from alembic import op

revision = "20261026_000000"
down_revision = "20260907_000000_drop_messages_thread_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_github_events_guild_repo_pr_id",
            "github_events",
            ["guild_id", "repo", "pr_number", "id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_github_events_guild_repo_pr_id",
            table_name="github_events",
            postgresql_concurrently=True,
        )
