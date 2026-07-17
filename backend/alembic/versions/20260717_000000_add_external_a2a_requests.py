"""Add replay-protected external A2A request audit records.

Revision ID: 20260717_000000_add_external_a2a_requests
Revises: 20260714_000000_add_task_logs_worker_agent_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260717_000000_add_external_a2a_requests"
down_revision: str | Sequence[str] | None = "20260714_000000_add_task_logs_worker_agent_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_a2a_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.Integer(), nullable=False),
        sa.Column("replay_key", sa.String(), nullable=False),
        sa.Column("external_message_id", sa.String(), nullable=False),
        sa.Column("sender_fqdn", sa.String(), nullable=False),
        sa.Column("governance_id", sa.String(), nullable=False),
        sa.Column("dnssec_state", sa.String(), nullable=False),
        sa.Column("dnsid_status", sa.String(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("skill", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="accepted", nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id",
            "sender_fqdn",
            "external_message_id",
            name="uq_external_a2a_sender_message",
        ),
    )
    op.create_index("ix_external_a2a_requests_guild_id", "external_a2a_requests", ["guild_id"])
    op.create_index(
        "ix_external_a2a_requests_sender_fqdn", "external_a2a_requests", ["sender_fqdn"]
    )
    op.create_index(
        "ix_external_a2a_requests_replay_key",
        "external_a2a_requests",
        ["replay_key"],
        unique=True,
    )
    op.create_index("ix_external_a2a_requests_task_id", "external_a2a_requests", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_external_a2a_requests_task_id", table_name="external_a2a_requests")
    op.drop_index("ix_external_a2a_requests_replay_key", table_name="external_a2a_requests")
    op.drop_index("ix_external_a2a_requests_sender_fqdn", table_name="external_a2a_requests")
    op.drop_index("ix_external_a2a_requests_guild_id", table_name="external_a2a_requests")
    op.drop_table("external_a2a_requests")
