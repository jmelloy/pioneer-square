"""add_guild_github_app_installation

Adds ``guilds.github_app_installation_id`` — the GitHub App installation id for
each guild's account/org. One App serves every guild; only the installation id
varies. NULL means fall back to the process-wide ``GITHUB_APP_INSTALLATION_ID``
env var (single-tenant deploys).

Revision ID: 20260725_000000_add_guild_github_app_installation
Revises: 20260723_000000_add_guild_spawn_defaults
Create Date: 2026-07-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260725_000000_add_guild_github_app_installation"
down_revision: str | Sequence[str] | None = "20260723_000000_add_guild_spawn_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("guilds", sa.Column("github_app_installation_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("guilds", "github_app_installation_id")
