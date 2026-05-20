"""drop_guild_id_child_tables

Add ``guild_pk`` to ``tasks`` and ``foreman_turns`` (with NOT NULL backfill),
then drop the legacy ``guild_id`` TEXT column from all nine child tables.

``guild_members`` gets a new composite PK of ``(guild_pk, user_id)`` replacing
the old ``(guild_id, user_id)``.  ``claude_credentials`` and ``guild_keys``
move their UNIQUE constraint from ``guild_id`` to ``guild_pk``.

Revision ID: 20260512_000002_drop_guild_id_child_tables
Revises: 20260512_000001_fk_child_tables_reference_guilds_int_pk
Create Date: 2026-05-12 00:02:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260512_000002_drop_guild_id_child_tables"
down_revision: str | Sequence[str] | None = "20260512_000001_fk_child_tables_reference_guilds_int_pk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Step 1: Re-backfill guild_pk for rows inserted after 20260512_000001
    # (application code was still writing guild_id only, so new rows have
    # guild_pk = NULL until this migration runs).
    # ------------------------------------------------------------------
    for table in [
        "agents",
        "messages",
        "workers",
        "guild_members",
        "claude_credentials",
        "guild_keys",
        "github_events",
    ]:
        op.execute(
            sa.text(
                f"UPDATE {table} "
                f"SET guild_pk = (SELECT id FROM guilds WHERE guilds.guild_id = {table}.guild_id) "
                f"WHERE guild_pk IS NULL"
            )
        )

    # ------------------------------------------------------------------
    # Step 2: Add guild_pk to tables that don't have it yet
    # ------------------------------------------------------------------
    op.execute(sa.text("ALTER TABLE tasks ADD COLUMN guild_pk INTEGER REFERENCES guilds(id)"))
    op.execute(
        sa.text(
            "UPDATE tasks "
            "SET guild_pk = (SELECT id FROM guilds WHERE guilds.guild_id = tasks.guild_id)"
        )
    )

    op.execute(
        sa.text("ALTER TABLE foreman_turns ADD COLUMN guild_pk INTEGER REFERENCES guilds(id)")
    )
    op.execute(
        sa.text(
            "UPDATE foreman_turns "
            "SET guild_pk = (SELECT id FROM guilds WHERE guilds.guild_id = foreman_turns.guild_id)"
        )
    )

    # ------------------------------------------------------------------
    # Step 3: Recreate each child table with guild_pk NOT NULL, no guild_id.
    # Ordering respects FK dependencies: workers → agents/tasks → github_events.
    # ------------------------------------------------------------------

    # workers
    op.execute(
        sa.text("""
        CREATE TABLE workers_new (
            id          TEXT NOT NULL PRIMARY KEY,
            guild_pk    INTEGER NOT NULL REFERENCES guilds(id),
            repos       TEXT NOT NULL DEFAULT '[]',
            org         TEXT,
            state       TEXT NOT NULL DEFAULT 'idle',
            created_at  TEXT NOT NULL,
            last_seen   TEXT,
            user_id     TEXT REFERENCES users(id),
            auth_token  TEXT
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO workers_new "
            "(id, guild_pk, repos, org, state, created_at, last_seen, user_id, auth_token) "
            "SELECT id, guild_pk, repos, org, state, created_at, last_seen, user_id, auth_token "
            "FROM workers"
        )
    )
    # agents.worker_id and tasks.worker_id FK reference workers.id; CASCADE
    # drops those constraints so the table can be dropped in PostgreSQL.
    # Migration 000002 recreates both tables with proper FK columns.
    op.execute(sa.text("DROP TABLE workers CASCADE"))
    op.rename_table("workers_new", "workers")

    # agents
    op.execute(
        sa.text("""
        CREATE TABLE agents_new (
            id          TEXT NOT NULL PRIMARY KEY,
            guild_pk    INTEGER NOT NULL REFERENCES guilds(id),
            worker_id   TEXT REFERENCES workers(id),
            name        TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'worker',
            state       TEXT NOT NULL DEFAULT 'idle',
            activity    TEXT,
            joined_at   TEXT NOT NULL,
            last_seen   TEXT
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO agents_new "
            "(id, guild_pk, worker_id, name, type, state, activity, joined_at, last_seen) "
            "SELECT id, guild_pk, worker_id, name, type, state, activity, joined_at, last_seen "
            "FROM agents"
        )
    )
    op.drop_table("agents")
    op.rename_table("agents_new", "agents")

    # tasks
    op.execute(
        sa.text("""
        CREATE TABLE tasks_new (
            id              TEXT NOT NULL PRIMARY KEY,
            worker_id       TEXT NOT NULL REFERENCES workers(id),
            guild_pk        INTEGER NOT NULL REFERENCES guilds(id),
            description     TEXT NOT NULL,
            tool            TEXT NOT NULL DEFAULT 'claude',
            issue_number    INTEGER,
            issue_repo      TEXT,
            state           TEXT NOT NULL DEFAULT 'pending',
            branch          TEXT,
            worktree_path   TEXT,
            pr_url          TEXT,
            pr_number       INTEGER,
            pr_repo         TEXT,
            created_at      TEXT NOT NULL,
            finished_at     TEXT,
            name            TEXT,
            parent_task_id  TEXT,
            phase           TEXT DEFAULT 'execute',
            deleted_at      TEXT,
            user_id         TEXT
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO tasks_new "
            "(id, worker_id, guild_pk, description, tool, issue_number, issue_repo, "
            "state, branch, worktree_path, pr_url, pr_number, pr_repo, created_at, "
            "finished_at, name, parent_task_id, phase, deleted_at, user_id) "
            "SELECT id, worker_id, guild_pk, description, tool, issue_number, issue_repo, "
            "state, branch, worktree_path, pr_url, pr_number, pr_repo, created_at, "
            "finished_at, name, parent_task_id, phase, deleted_at, user_id FROM tasks"
        )
    )
    # task_logs.task_id FK references tasks.id; CASCADE drops it so the table
    # can be replaced.  task_logs is not touched by this migration so its
    # task_id column persists (just without the FK constraint).
    op.execute(sa.text("DROP TABLE tasks CASCADE"))
    op.rename_table("tasks_new", "tasks")

    # messages
    op.execute(
        sa.text("""
        CREATE TABLE messages_new (
            id              INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            guild_pk        INTEGER NOT NULL REFERENCES guilds(id),
            from_agent      TEXT,
            to_agent        TEXT,
            content         TEXT NOT NULL,
            message_type    TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            user_id         TEXT
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO messages_new "
            "(id, guild_pk, from_agent, to_agent, content, message_type, created_at, user_id) "
            "SELECT id, guild_pk, from_agent, to_agent, content, message_type, created_at, user_id "
            "FROM messages"
        )
    )
    op.drop_table("messages")
    op.rename_table("messages_new", "messages")

    # guild_members — new composite PK: (guild_pk, user_id)
    op.execute(
        sa.text("""
        CREATE TABLE guild_members_new (
            guild_pk    INTEGER NOT NULL REFERENCES guilds(id),
            user_id     TEXT NOT NULL REFERENCES users(id),
            role        TEXT NOT NULL DEFAULT 'member',
            created_at  TEXT NOT NULL,
            PRIMARY KEY (guild_pk, user_id)
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO guild_members_new (guild_pk, user_id, role, created_at) "
            "SELECT guild_pk, user_id, role, created_at FROM guild_members"
        )
    )
    op.drop_table("guild_members")
    op.rename_table("guild_members_new", "guild_members")

    # claude_credentials — UNIQUE moves from guild_id to guild_pk
    op.execute(
        sa.text("""
        CREATE TABLE claude_credentials_new (
            id                  INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            guild_pk            INTEGER NOT NULL UNIQUE REFERENCES guilds(id),
            credentials_blob    TEXT NOT NULL,
            updated_at          TEXT NOT NULL
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO claude_credentials_new (id, guild_pk, credentials_blob, updated_at) "
            "SELECT id, guild_pk, credentials_blob, updated_at FROM claude_credentials"
        )
    )
    op.drop_table("claude_credentials")
    op.rename_table("claude_credentials_new", "claude_credentials")

    # guild_keys — UNIQUE moves from guild_id to guild_pk
    op.execute(
        sa.text("""
        CREATE TABLE guild_keys_new (
            id                  INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            guild_pk            INTEGER NOT NULL UNIQUE REFERENCES guilds(id),
            key_id              TEXT NOT NULL,
            public_key_pem      TEXT NOT NULL,
            private_key_pem     TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            custom_jwks         TEXT,
            private_key_jwk     TEXT
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO guild_keys_new "
            "(id, guild_pk, key_id, public_key_pem, private_key_pem, "
            "created_at, custom_jwks, private_key_jwk) "
            "SELECT id, guild_pk, key_id, public_key_pem, private_key_pem, "
            "created_at, custom_jwks, private_key_jwk FROM guild_keys"
        )
    )
    op.drop_table("guild_keys")
    op.rename_table("guild_keys_new", "guild_keys")

    # github_events — FK to tasks (recreated above)
    op.execute(
        sa.text("""
        CREATE TABLE github_events_new (
            id              INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            guild_pk        INTEGER NOT NULL REFERENCES guilds(id),
            task_id         TEXT REFERENCES tasks(id),
            delivery_id     TEXT NOT NULL UNIQUE,
            event_type      TEXT NOT NULL,
            action          TEXT,
            repo            TEXT NOT NULL,
            pr_number       INTEGER,
            pr_url          TEXT,
            sender_login    TEXT,
            payload_json    TEXT NOT NULL,
            created_at      TEXT NOT NULL
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO github_events_new "
            "(id, guild_pk, task_id, delivery_id, event_type, action, "
            "repo, pr_number, pr_url, sender_login, payload_json, created_at) "
            "SELECT id, guild_pk, task_id, delivery_id, event_type, action, "
            "repo, pr_number, pr_url, sender_login, payload_json, created_at "
            "FROM github_events"
        )
    )
    op.drop_table("github_events")
    op.rename_table("github_events_new", "github_events")

    # foreman_turns — self-referential parent_id (not enforced by SQLite)
    op.execute(
        sa.text("""
        CREATE TABLE foreman_turns_new (
            id              INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            guild_pk        INTEGER NOT NULL REFERENCES guilds(id),
            user_id         TEXT NOT NULL,
            role            TEXT NOT NULL,
            content_json    TEXT NOT NULL,
            is_tool_response INTEGER NOT NULL DEFAULT 0,
            parent_id       INTEGER,
            created_at      TEXT NOT NULL
        )
        """)
    )
    op.execute(
        sa.text(
            "INSERT INTO foreman_turns_new "
            "(id, guild_pk, user_id, role, content_json, is_tool_response, parent_id, created_at) "
            "SELECT id, guild_pk, user_id, role, content_json, is_tool_response, parent_id, created_at "
            "FROM foreman_turns"
        )
    )
    op.drop_table("foreman_turns")
    op.rename_table("foreman_turns_new", "foreman_turns")


def downgrade() -> None:
    # This migration restructured every major table (workers, agents, tasks,
    # messages, guild_members, …) to remove the legacy guild_id TEXT column
    # and make guild_pk NOT NULL.  Reversing it on PostgreSQL would require
    # restoring guild_id from the guilds lookup join on every table in the
    # correct FK-dependency order, then dropping the NOT NULL constraint.
    # That path was never tested and is not supported for this Postgres
    # deployment — roll forward instead.
    raise NotImplementedError(
        "Downgrade of 20260512_000002 is not supported.  "
        "The schema restructuring that dropped guild_id from child tables "
        "cannot be safely reversed on PostgreSQL.  Roll forward instead."
    )
