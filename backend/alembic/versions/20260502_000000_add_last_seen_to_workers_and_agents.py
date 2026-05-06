"""add_last_seen_to_workers_and_agents

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-05-02 00:00:00.000000

"""

from datetime import UTC, datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("last_seen", sa.Text(), nullable=True))
    with op.batch_alter_table("workers") as batch_op:
        batch_op.add_column(sa.Column("last_seen", sa.Text(), nullable=True))

    # Backfill existing rows with the migration timestamp so the stale-worker
    # sweeper doesn't mark every pre-existing worker offline the moment it
    # runs. Real workers will refresh this within seconds via their
    # heartbeat; ones that don't reconnect will age out naturally.
    now = datetime.now(UTC).isoformat()
    op.execute(sa.text("UPDATE workers SET last_seen = :now").bindparams(now=now))
    op.execute(sa.text("UPDATE agents SET last_seen = :now").bindparams(now=now))


def downgrade() -> None:
    with op.batch_alter_table("workers") as batch_op:
        batch_op.drop_column("last_seen")
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("last_seen")
