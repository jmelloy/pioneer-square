"""guilds_integer_pk_soft_delete

Converts the guilds table from a text primary key to an integer surrogate
primary key, renames the existing text id to guild_id, and adds a
soft-delete column (deleted_at).  A partial unique index ensures that
guild_id is unique among *active* (non-deleted) rows, allowing the same
guild_id to be reused once a guild is soft-deleted.

Revision ID: 20260512_000000_guilds_integer_pk_soft_delete
Revises: 20260506_000003_add_a2a_fields_to_guilds
Create Date: 2026-05-12 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260512_000000_guilds_integer_pk_soft_delete"
down_revision: str | Sequence[str] | None = "20260506_000003_add_a2a_fields_to_guilds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite does not allow altering primary key columns, so we recreate the
    # table.  Child-table FK constraints reference guilds.guild_id (text) and
    # their data values are unchanged, so no child-table rows need to move.
    # SQLite does not enforce FK constraints by default, so the drop/rename is
    # safe without disabling foreign keys.

    op.execute(
        sa.text("""
            CREATE TABLE guilds_new (
                id         INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                guild_id   TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                name       TEXT,
                github_user_id TEXT,
                primary_repo   TEXT,
                webhook_secret TEXT,
                description    TEXT,
                url            TEXT,
                version        TEXT,
                deleted_at     TEXT
            )
        """)
    )

    op.execute(
        sa.text("""
            INSERT INTO guilds_new
                (guild_id, created_at, name, github_user_id, primary_repo,
                 webhook_secret, description, url, version)
            SELECT id, created_at, name, github_user_id, primary_repo,
                   webhook_secret, description, url, version
            FROM guilds
        """)
    )

    op.drop_table("guilds")
    op.rename_table("guilds_new", "guilds")

    # Partial unique index: only one active (deleted_at IS NULL) guild per guild_id.
    op.create_index(
        "uq_guilds_guild_id_active",
        "guilds",
        ["guild_id"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_guilds_guild_id_active", table_name="guilds")

    op.execute(
        sa.text("""
            CREATE TABLE guilds_old (
                id             TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                name           TEXT,
                github_user_id TEXT,
                primary_repo   TEXT,
                webhook_secret TEXT,
                description    TEXT,
                url            TEXT,
                version        TEXT,
                PRIMARY KEY (id)
            )
        """)
    )

    # Only restore active (non-deleted) guilds; soft-deleted rows are lost.
    op.execute(
        sa.text("""
            INSERT INTO guilds_old
                (id, created_at, name, github_user_id, primary_repo,
                 webhook_secret, description, url, version)
            SELECT guild_id, created_at, name, github_user_id, primary_repo,
                   webhook_secret, description, url, version
            FROM guilds
            WHERE deleted_at IS NULL
        """)
    )

    op.drop_table("guilds")
    op.rename_table("guilds_old", "guilds")
