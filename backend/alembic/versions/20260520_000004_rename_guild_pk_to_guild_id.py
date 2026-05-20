"""rename_guild_pk_to_guild_id

Rename the ``guild_pk`` FK column to ``guild_id`` in all child tables.

``guild_pk`` was a transitional name introduced in migration
20260512_000001 to distinguish the new integer FK column from the legacy
TEXT ``guild_id`` column that was subsequently dropped in 20260512_000002.
Now that the schema is fully migrated, the conventional FK name
``guild_id`` (pointing at ``guilds.id``) is used consistently everywhere.

Revision ID: 20260520_000004_rename_guild_pk_to_guild_id
Revises: 20260520_000003_merge_locking_and_messages
Create Date: 2026-05-20 00:00:04.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_000004_rename_guild_pk_to_guild_id"
down_revision: str | Sequence[str] | None = "20260520_000003_merge_locking_and_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables with a single FK on guild_pk (no extra unique constraints).
# guild_members also has guild_pk as part of the composite PK; PostgreSQL
# keeps the PK constraint valid across the rename automatically.
_SIMPLE_FK_TABLES = [
    "agents",
    "messages",
    "workers",
    "tasks",
    "guild_members",
    "foreman_turns",
    "github_events",
]

# Tables where guild_pk carries both a FK and a UNIQUE constraint.
_UNIQUE_FK_TABLES = [
    "claude_credentials",
    "guild_keys",
]


def _drop_fk(table: str, col: str) -> None:
    """Drop the FK on *col* from *table*, robust to auto-generated constraint names.

    Migration 20260512_000002 recreated tables as ``{table}_new`` then renamed
    them.  PostgreSQL keeps the original auto-generated constraint name
    (e.g. ``agents_new_guild_pk_fkey``) after a table rename, so we cannot
    assume the conventional ``{table}_{col}_fkey`` pattern.  We query
    ``pg_constraint`` at migration time to find and drop whatever name exists.
    """
    op.execute(
        sa.text(f"""
        DO $$ DECLARE cname TEXT; BEGIN
            SELECT c.conname INTO cname
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = '{table}'
               AND c.contype = 'f'
               AND c.conname LIKE '%{col}_fkey';
            IF cname IS NOT NULL THEN
                EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', cname);
            END IF;
        END $$
        """)
    )


def _drop_unique(table: str, col: str) -> None:
    """Drop the UNIQUE constraint on *col* from *table*, robust to auto-generated names."""
    op.execute(
        sa.text(f"""
        DO $$ DECLARE cname TEXT; BEGIN
            SELECT c.conname INTO cname
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = '{table}'
               AND c.contype = 'u'
               AND c.conname LIKE '%{col}_key';
            IF cname IS NOT NULL THEN
                EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', cname);
            END IF;
        END $$
        """)
    )


def upgrade() -> None:
    for table in _SIMPLE_FK_TABLES:
        _drop_fk(table, "guild_pk")
        op.alter_column(table, "guild_pk", new_column_name="guild_id")
        op.create_foreign_key(f"{table}_guild_id_fkey", table, "guilds", ["guild_id"], ["id"])

    for table in _UNIQUE_FK_TABLES:
        _drop_fk(table, "guild_pk")
        _drop_unique(table, "guild_pk")
        op.alter_column(table, "guild_pk", new_column_name="guild_id")
        op.create_unique_constraint(f"{table}_guild_id_key", table, ["guild_id"])
        op.create_foreign_key(f"{table}_guild_id_fkey", table, "guilds", ["guild_id"], ["id"])


def downgrade() -> None:
    for table in reversed(_UNIQUE_FK_TABLES):
        op.drop_constraint(f"{table}_guild_id_fkey", table, type_="foreignkey")
        op.drop_constraint(f"{table}_guild_id_key", table, type_="unique")
        op.alter_column(table, "guild_id", new_column_name="guild_pk")
        op.create_unique_constraint(f"{table}_guild_pk_key", table, ["guild_pk"])
        op.create_foreign_key(f"{table}_guild_pk_fkey", table, "guilds", ["guild_pk"], ["id"])

    for table in reversed(_SIMPLE_FK_TABLES):
        op.drop_constraint(f"{table}_guild_id_fkey", table, type_="foreignkey")
        op.alter_column(table, "guild_id", new_column_name="guild_pk")
        op.create_foreign_key(f"{table}_guild_pk_fkey", table, "guilds", ["guild_pk"], ["id"])
