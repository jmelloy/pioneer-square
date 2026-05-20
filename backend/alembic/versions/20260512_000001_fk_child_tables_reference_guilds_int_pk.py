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
        # SQLite supports REFERENCES in ALTER TABLE ADD COLUMN (3.26+).
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
    # SQLite 3.35+ supports DROP COLUMN.  For compatibility we recreate each
    # table without guild_pk instead of relying on ALTER TABLE DROP COLUMN.
    # The table DDL here must match the state *before* the upgrade step above.

    _snapshots: dict[str, str] = {
        "agents": """
            CREATE TABLE agents_old (
                id         TEXT NOT NULL PRIMARY KEY,
                guild_id   TEXT NOT NULL REFERENCES guilds(guild_id),
                worker_id  TEXT REFERENCES workers(id),
                name       TEXT NOT NULL,
                type       TEXT NOT NULL DEFAULT 'worker',
                state      TEXT NOT NULL DEFAULT 'idle',
                activity   TEXT,
                joined_at  TEXT NOT NULL,
                last_seen  TEXT
            )
        """,
        "messages": """
            CREATE TABLE messages_old (
                id           SERIAL PRIMARY KEY,
                guild_id     TEXT NOT NULL REFERENCES guilds(guild_id),
                from_agent   TEXT,
                to_agent     TEXT,
                content      TEXT NOT NULL,
                message_type TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                user_id      TEXT
            )
        """,
        "workers": """
            CREATE TABLE workers_old (
                id         TEXT NOT NULL PRIMARY KEY,
                guild_id   TEXT NOT NULL REFERENCES guilds(guild_id),
                repos      TEXT NOT NULL DEFAULT '[]',
                org        TEXT,
                state      TEXT NOT NULL DEFAULT 'idle',
                created_at TEXT NOT NULL,
                last_seen  TEXT,
                user_id    TEXT REFERENCES users(id),
                auth_token TEXT
            )
        """,
        "guild_members": """
            CREATE TABLE guild_members_old (
                guild_id   TEXT NOT NULL REFERENCES guilds(guild_id),
                user_id    TEXT NOT NULL REFERENCES users(id),
                role       TEXT NOT NULL DEFAULT 'member',
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """,
        "claude_credentials": """
            CREATE TABLE claude_credentials_old (
                id                SERIAL PRIMARY KEY,
                guild_id          TEXT NOT NULL UNIQUE REFERENCES guilds(guild_id),
                credentials_blob  TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            )
        """,
        "guild_keys": """
            CREATE TABLE guild_keys_old (
                id              SERIAL PRIMARY KEY,
                guild_id        TEXT NOT NULL UNIQUE REFERENCES guilds(guild_id),
                key_id          TEXT NOT NULL,
                public_key_pem  TEXT NOT NULL,
                private_key_pem TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                custom_jwks     TEXT,
                private_key_jwk TEXT
            )
        """,
        "github_events": """
            CREATE TABLE github_events_old (
                id           SERIAL PRIMARY KEY,
                guild_id     TEXT NOT NULL REFERENCES guilds(guild_id),
                task_id      TEXT REFERENCES tasks(id),
                delivery_id  TEXT NOT NULL UNIQUE,
                event_type   TEXT NOT NULL,
                action       TEXT,
                repo         TEXT NOT NULL,
                pr_number    INTEGER,
                pr_url       TEXT,
                sender_login TEXT,
                payload_json TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
        """,
    }

    # Columns to copy (all except guild_pk).
    _columns: dict[str, str] = {
        "agents": "id, guild_id, worker_id, name, type, state, activity, joined_at, last_seen",
        "messages": "id, guild_id, from_agent, to_agent, content, message_type, created_at, user_id",
        "workers": "id, guild_id, repos, org, state, created_at, last_seen, user_id, auth_token",
        "guild_members": "guild_id, user_id, role, created_at",
        "claude_credentials": "id, guild_id, credentials_blob, updated_at",
        "guild_keys": "id, guild_id, key_id, public_key_pem, private_key_pem, created_at, custom_jwks, private_key_jwk",
        "github_events": "id, guild_id, task_id, delivery_id, event_type, action, repo, pr_number, pr_url, sender_login, payload_json, created_at",
    }

    for table in reversed(_FK_TABLES):
        cols = _columns[table]
        op.execute(sa.text(_snapshots[table]))
        op.execute(sa.text(f"INSERT INTO {table}_old ({cols}) SELECT {cols} FROM {table}"))
        op.drop_table(table)
        op.rename_table(f"{table}_old", table)
