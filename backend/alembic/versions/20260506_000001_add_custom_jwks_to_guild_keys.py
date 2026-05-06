"""add_custom_jwks_to_guild_keys

Adds ``custom_jwks`` (public JWKS JSON served at /.well-known/jwks.json when
set) and ``private_key_jwk`` (private key JWK for backend signing, never
served) to the ``guild_keys`` table.

Revision ID: 20260506_000001_add_custom_jwks_to_guild_keys
Revises: 20260506_000000_add_guild_keys
Create Date: 2026-05-06 00:01:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260506_000001_add_custom_jwks_to_guild_keys"
down_revision: str | Sequence[str] | None = "20260506_000000_add_guild_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("guild_keys") as batch_op:
        batch_op.add_column(sa.Column("custom_jwks", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("private_key_jwk", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("guild_keys") as batch_op:
        batch_op.drop_column("private_key_jwk")
        batch_op.drop_column("custom_jwks")
