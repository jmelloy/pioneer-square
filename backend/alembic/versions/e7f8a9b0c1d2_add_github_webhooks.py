"""add_github_webhooks

Adds the plumbing for receiving GitHub webhook deliveries:

- ``guilds.webhook_secret``: HMAC-SHA256 shared secret used to verify
  incoming webhook bodies. Generated lazily when an owner first requests it.
- ``tasks.pr_number`` / ``tasks.pr_repo``: explicit PR coordinates extracted
  at PR-creation time so events can be linked back to the originating task
  without fragile URL string matching.
- ``github_events``: append-only log of accepted webhook deliveries. The
  ``delivery_id`` unique constraint short-circuits GitHub's redelivery on
  non-2xx responses so the foreman is never invoked twice for the same event.

Revision ID: e7f8a9b0c1d2
Revises: c7d8e9f0a1b2
Create Date: 2026-05-06 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("guilds") as batch_op:
        batch_op.add_column(sa.Column("webhook_secret", sa.Text(), nullable=True))

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("pr_number", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("pr_repo", sa.Text(), nullable=True))

    op.create_table(
        "github_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.Text(), sa.ForeignKey("guilds.id"), nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("delivery_id", sa.Text(), nullable=False, unique=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("sender_login", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_github_events_task_id", "github_events", ["task_id"])
    op.create_index("ix_github_events_guild_id", "github_events", ["guild_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_github_events_guild_id", table_name="github_events")
    op.drop_index("ix_github_events_task_id", table_name="github_events")
    op.drop_table("github_events")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("pr_repo")
        batch_op.drop_column("pr_number")
    with op.batch_alter_table("guilds") as batch_op:
        batch_op.drop_column("webhook_secret")
