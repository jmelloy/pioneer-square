"""add_tokens_to_foreman_turns

Adds ``input_tokens`` and ``output_tokens`` columns to ``foreman_turns`` so
the token usage returned by the Anthropic API can be stored per assistant turn.
Both columns are nullable integers; existing rows and non-assistant turns
(user/system) will have NULL values.

Revision ID: 20260515_000001_add_tokens_to_foreman_turns
Revises: 20260515_000000_add_name_to_workers
Create Date: 2026-05-15 00:01:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260515_000001_add_tokens_to_foreman_turns"
down_revision: str | Sequence[str] | None = "20260515_000000_add_name_to_workers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("foreman_turns") as batch_op:
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("foreman_turns") as batch_op:
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
