"""add_last_refreshed_at_to_github_cache

Adds ``last_refreshed_at`` to ``github_issues`` and ``github_pull_requests``,
tracking when we last polled GitHub for that row (distinct from GitHub's own
``updated_at``, which only reflects when *GitHub's* state last changed). The
periodic refresh sweep in foreman.runner uses this column to throttle refetches
of cached issues/PRs linked only by terminal tasks, so the task tree view no
longer shows indefinitely-stale status for issues/PRs whose tasks finished
before the underlying GitHub state changed.

Revision ID: 20260728_000000_add_last_refreshed_at_to_github_cache
Revises: 20260725_000000_add_guild_github_app_installation
Create Date: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260728_000000_add_last_refreshed_at_to_github_cache"
down_revision: str | Sequence[str] | None = "20260725_000000_add_guild_github_app_installation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "github_issues",
        sa.Column(
            "last_refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "github_pull_requests",
        sa.Column(
            "last_refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("github_pull_requests", "last_refreshed_at")
    op.drop_column("github_issues", "last_refreshed_at")
