"""add_guild_keys

Adds a ``guild_keys`` table to store per-guild EC P-256 key pairs used to
serve guild-specific JWKS at /.well-known/jwks.json on subdomain requests.

Revision ID: 20260506_000000_add_guild_keys
Revises: f4a5b6c7d8e9
Create Date: 2026-05-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260506_000000_add_guild_keys"
down_revision: str | Sequence[str] | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guild_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("private_key_pem", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id"),
    )


def downgrade() -> None:
    op.drop_table("guild_keys")
