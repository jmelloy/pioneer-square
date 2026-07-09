"""Add github_issues and github_pull_requests tables.

Revision ID: 20260709_000000_add_github_issues_and_pull_requests
Revises: 20260708_000001_add_discord_thread_id_to_tasks
Create Date: 2026-07-09

Local DB cache of GitHub issue/PR state (#864). Webhooks upsert on push;
status/polling calls read from and update these tables. Both tables are
keyed unique on (repo, number) so a repo/number pair maps to exactly one
row and upserts from either the webhook receiver or
scripts/backfill_github_cache.py are idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260709_000000_add_github_issues_and_pull_requests"
down_revision: str | Sequence[str] | None = "20260708_000001_add_discord_thread_id_to_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_issues",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("assignees", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("milestone", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", JSONB(), nullable=False),
    )
    op.create_index(
        "uq_github_issues_repo_number", "github_issues", ["repo", "number"], unique=True
    )

    op.create_table(
        "github_pull_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("merged", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("draft", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("head_sha", sa.Text(), nullable=True),
        sa.Column("head_ref", sa.Text(), nullable=True),
        sa.Column("base_ref", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("assignees", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", JSONB(), nullable=False),
    )
    op.create_index(
        "uq_github_pull_requests_repo_number",
        "github_pull_requests",
        ["repo", "number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_github_pull_requests_repo_number", table_name="github_pull_requests")
    op.drop_table("github_pull_requests")
    op.drop_index("uq_github_issues_repo_number", table_name="github_issues")
    op.drop_table("github_issues")
