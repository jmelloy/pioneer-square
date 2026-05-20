"""fk_child_tables_reference_guilds_int_pk

Add ``guild_pk INTEGER REFERENCES guilds(id)`` to every table that
previously declared a TEXT FK to ``guilds(guild_id)`` (which is no longer the
primary key after the integer-PK migration).

The existing ``guild_id TEXT`` column is **retained** for backwards-compatible
application queries that filter by the human-readable guild identifier.
``guild_pk`` provides proper referential integrity against the new integer PK
and is backfilled for all existing rows.

Tables updated: agents, messages, workers, guild_members,
                claude_credentials, guild_keys, github_events.

Revision ID: 20260512_000001_fk_child_tables_reference_guilds_int_pk
Revises: 20260512_000000_guilds_integer_pk_soft_delete
Create Date: 2026-05-12 00:01:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260512_000001_fk_child_tables_reference_guilds_int_pk"
down_revision: str | Sequence[str] | None = "20260512_000000_guilds_integer_pk_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables whose guild_id TEXT column previously carried ForeignKey("guilds.guild_id").
# We add guild_pk INTEGER REFERENCES guilds(id) to each and backfill it.
_FK_TABLES = [
    "agents",
    "messages",
    "workers",
    "guild_members",
    "claude_credentials",
    "guild_keys",
    "github_events",
]


def upgrade() -> None:
    for table in _FK_TABLES:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD COLUMN guild_pk INTEGER REFERENCES guilds(id)"
            )
        )
        # Backfill: match on the TEXT guild_id that both tables share.
        op.execute(
            sa.text(
                f"UPDATE {table} "
                f"SET guild_pk = (SELECT id FROM guilds WHERE guilds.guild_id = {table}.guild_id)"
            )
        )


def downgrade() -> None:
    # The upgrade just added a nullable guild_pk column to each table.
    # Reversing it is a simple column drop — no table recreation needed.
    for table in _FK_TABLES:
        op.drop_column(table, "guild_pk")
