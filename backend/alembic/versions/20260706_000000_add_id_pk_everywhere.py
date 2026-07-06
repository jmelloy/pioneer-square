"""Add a surrogate `id` primary key to every table that lacks one.

Revision ID: 20260706_000000_add_id_pk_everywhere
Revises: 20260705_000001_add_discord_task_threads
Create Date: 2026-07-06

Audit (issue #791) found eight tables whose primary key was either a natural
string column or a composite of two columns instead of a plain `id`:

- github_tokens        (PK was github_user_id)
- user_sessions        (PK was token)
- guild_members        (PK was (guild_id, user_id))
- user_spawn_settings  (PK was (guild_id, user_id))
- push_tokens          (PK was token)
- discord_users        (PK was github_login)
- discord_connect_tokens (PK was token)
- model_catalog        (PK was (provider, model_id))

For each, this adds an identity `id` column, makes it the primary key, and
preserves the old key as a UNIQUE constraint/index so existing lookups,
foreign keys, and ON CONFLICT upsert targets keep working unchanged (Postgres
resolves `ON CONFLICT (col, ...)` against any matching unique constraint or
index, not just the primary key).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260706_000000_add_id_pk_everywhere"
down_revision: str | Sequence[str] | None = "20260705_000001_add_discord_task_threads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, old pk columns, unique constraint/index name, is_composite)
_SINGLE_COLUMN_TABLES = [
    ("github_tokens", "github_user_id"),
    ("user_sessions", "token"),
    ("push_tokens", "token"),
    ("discord_users", "github_login"),
    ("discord_connect_tokens", "token"),
]

_COMPOSITE_TABLES = [
    ("guild_members", ["guild_id", "user_id"], "uq_guild_members_guild_id_user_id"),
    ("user_spawn_settings", ["guild_id", "user_id"], "uq_user_spawn_settings_guild_id_user_id"),
    ("model_catalog", ["provider", "model_id"], "uq_model_catalog_provider_model_id"),
]


def _pk_constraint_name(table: str) -> str:
    """Look up a table's actual primary-key constraint name.

    Some tables (e.g. ``guild_members``) went through a create-as-``_new``-
    then-rename migration (20260512_000002); PostgreSQL keeps the
    constraint's original auto-generated name (``guild_members_new_pkey``)
    across the rename, so we can't assume ``{table}_pkey``.

    Only call this to resolve the *original* (pre-migration) PK name, before
    any constraint changes have been made. Once this migration has created
    the surrogate ``id`` PK, its name is always ``{table}_pkey`` (set
    explicitly by ``op.create_primary_key`` below) — downgrade() hardcodes
    that rather than re-querying, since SQLite/test DBs can return a PK
    reflection with no name at all.
    """
    bind = op.get_bind()
    pk = sa.inspect(bind).get_pk_constraint(table)
    return pk["name"] if pk and pk.get("name") else f"{table}_pkey"


def upgrade() -> None:
    # user_sessions.github_user_id FKs to github_tokens.github_user_id; drop
    # that FK before github_tokens' PK constraint changes, then recreate it.
    op.drop_constraint("user_sessions_github_user_id_fkey", "user_sessions", type_="foreignkey")

    for table, col in _SINGLE_COLUMN_TABLES:
        old_pk_name = _pk_constraint_name(table)
        op.add_column(table, sa.Column("id", sa.Integer(), sa.Identity(), nullable=False))
        op.drop_constraint(old_pk_name, table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, ["id"])
        op.create_unique_constraint(f"{table}_{col}_key", table, [col])

    op.create_foreign_key(
        "user_sessions_github_user_id_fkey",
        "user_sessions",
        "github_tokens",
        ["github_user_id"],
        ["github_user_id"],
    )

    for table, cols, index_name in _COMPOSITE_TABLES:
        old_pk_name = _pk_constraint_name(table)
        op.add_column(table, sa.Column("id", sa.Integer(), sa.Identity(), nullable=False))
        op.drop_constraint(old_pk_name, table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, ["id"])
        op.create_index(index_name, table, cols, unique=True)


def downgrade() -> None:
    # The surrogate PK created above is always named "{table}_pkey" (set
    # explicitly by op.create_primary_key in upgrade()); hardcode it here
    # rather than re-inspecting, since by this point the original PK name
    # has already been replaced and no longer exists to look up.
    for table, cols, index_name in reversed(_COMPOSITE_TABLES):
        op.drop_index(index_name, table_name=table)
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, cols)
        op.drop_column(table, "id")

    op.drop_constraint("user_sessions_github_user_id_fkey", "user_sessions", type_="foreignkey")

    for table, col in reversed(_SINGLE_COLUMN_TABLES):
        op.drop_constraint(f"{table}_{col}_key", table, type_="unique")
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, [col])
        op.drop_column(table, "id")

    op.create_foreign_key(
        "user_sessions_github_user_id_fkey",
        "user_sessions",
        "github_tokens",
        ["github_user_id"],
        ["github_user_id"],
    )
