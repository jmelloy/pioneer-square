"""Add api_request_log table.

Revision ID: 20260605_000002_add_api_request_log
Revises: 20260605_000001_add_api_calls_to_foreman_turns
Create Date: 2026-06-05

Records every Anthropic messages.create() call made by the foreman: the full
request (model, system prompt, messages, extra params) and the full response
(request_id header, response body, token usage, stop_reason). Created before
the API call; updated after. foreman_turns.request_id FK points here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260605_000002_add_api_request_log"
down_revision: str | Sequence[str] | None = "20260605_000001_add_api_calls_to_foreman_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_request_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Anthropic x-request-id response header; nullable until the call completes.
        sa.Column("request_id", sa.Text(), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        # Full JSON of the system blocks sent (serialized list of content block dicts).
        sa.Column("system", sa.Text(), nullable=True),
        # Full outbound messages array as JSONB.
        sa.Column("messages", sa.JSON(), nullable=True),
        # Full response body from Anthropic as JSONB.
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id"), nullable=True),
        # Extra API parameters: max_tokens, tools, tool_choice, etc.
        sa.Column("extra", sa.JSON(), nullable=True),
    )
    op.create_index("ix_api_request_log_task_id", "api_request_log", ["task_id"])
    op.create_index("ix_api_request_log_created_at", "api_request_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_api_request_log_created_at", table_name="api_request_log")
    op.drop_index("ix_api_request_log_task_id", table_name="api_request_log")
    op.drop_table("api_request_log")
