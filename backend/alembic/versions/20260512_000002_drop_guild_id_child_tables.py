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
    op.drop_table("workers")
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
    op.drop_table("tasks")
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
    # Restore guild_id by joining through guilds(id) → guilds(guild_id).
    # guild_pk becomes nullable again (state of 20260512_000001).

    # foreman_turns
    op.create_table(
        "foreman_turns_old",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("is_tool_response", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_id", sa.Integer()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
    )
    op.execute(
        sa.text(
            "INSERT INTO foreman_turns_old "
            "(id, guild_id, user_id, role, content_json, is_tool_response, parent_id, created_at, guild_pk) "
            "SELECT ft.id, g.guild_id, ft.user_id, ft.role, ft.content_json, "
            "ft.is_tool_response, ft.parent_id, ft.created_at, ft.guild_pk "
            "FROM foreman_turns ft JOIN guilds g ON g.id = ft.guild_pk"
        )
    )
    op.drop_table("foreman_turns")
    op.rename_table("foreman_turns_old", "foreman_turns")

    # github_events
    op.create_table(
        "github_events_old",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id")),
        sa.Column("delivery_id", sa.Text(), nullable=False, unique=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("action", sa.Text()),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("pr_url", sa.Text()),
        sa.Column("sender_login", sa.Text()),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.execute(
        sa.text(
            "INSERT INTO github_events_old "
            "(id, guild_id, guild_pk, task_id, delivery_id, event_type, action, "
            "repo, pr_number, pr_url, sender_login, payload_json, created_at) "
            "SELECT ge.id, g.guild_id, ge.guild_pk, ge.task_id, ge.delivery_id, "
            "ge.event_type, ge.action, ge.repo, ge.pr_number, ge.pr_url, "
            "ge.sender_login, ge.payload_json, ge.created_at "
            "FROM github_events ge JOIN guilds g ON g.id = ge.guild_pk"
        )
    )
    op.drop_table("github_events")
    op.rename_table("github_events_old", "github_events")

    # guild_keys
    op.create_table(
        "guild_keys_old",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("guild_id", sa.Text(), nullable=False, unique=True),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("private_key_pem", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("custom_jwks", sa.Text()),
        sa.Column("private_key_jwk", sa.Text()),
    )
    op.execute(
        sa.text(
            "INSERT INTO guild_keys_old "
            "(id, guild_id, guild_pk, key_id, public_key_pem, private_key_pem, "
            "created_at, custom_jwks, private_key_jwk) "
            "SELECT gk.id, g.guild_id, gk.guild_pk, gk.key_id, gk.public_key_pem, "
            "gk.private_key_pem, gk.created_at, gk.custom_jwks, gk.private_key_jwk "
            "FROM guild_keys gk JOIN guilds g ON g.id = gk.guild_pk"
        )
    )
    op.drop_table("guild_keys")
    op.rename_table("guild_keys_old", "guild_keys")

    # claude_credentials
    op.create_table(
        "claude_credentials_old",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("guild_id", sa.Text(), nullable=False, unique=True),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
        sa.Column("credentials_blob", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.execute(
        sa.text(
            "INSERT INTO claude_credentials_old (id, guild_id, guild_pk, credentials_blob, updated_at) "
            "SELECT cc.id, g.guild_id, cc.guild_pk, cc.credentials_blob, cc.updated_at "
            "FROM claude_credentials cc JOIN guilds g ON g.id = cc.guild_pk"
        )
    )
    op.drop_table("claude_credentials")
    op.rename_table("claude_credentials_old", "claude_credentials")

    # guild_members — restore composite PK (guild_id, user_id)
    op.create_table(
        "guild_members_old",
        sa.Column("guild_id", sa.Text(), nullable=False, primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False, primary_key=True),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.execute(
        sa.text(
            "INSERT INTO guild_members_old (guild_id, user_id, guild_pk, role, created_at) "
            "SELECT g.guild_id, gm.user_id, gm.guild_pk, gm.role, gm.created_at "
            "FROM guild_members gm JOIN guilds g ON g.id = gm.guild_pk"
        )
    )
    op.drop_table("guild_members")
    op.rename_table("guild_members_old", "guild_members")

    # messages
    op.create_table(
        "messages_old",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
        sa.Column("from_agent", sa.Text()),
        sa.Column("to_agent", sa.Text()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text()),
    )
    op.execute(
        sa.text(
            "INSERT INTO messages_old "
            "(id, guild_id, guild_pk, from_agent, to_agent, content, message_type, created_at, user_id) "
            "SELECT m.id, g.guild_id, m.guild_pk, m.from_agent, m.to_agent, "
            "m.content, m.message_type, m.created_at, m.user_id "
            "FROM messages m JOIN guilds g ON g.id = m.guild_pk"
        )
    )
    op.drop_table("messages")
    op.rename_table("messages_old", "messages")

    # tasks — drop guild_pk column (restore guild_id)
    op.create_table(
        "tasks_old",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("worker_id", sa.Text(), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False, server_default="claude"),
        sa.Column("issue_number", sa.Integer()),
        sa.Column("issue_repo", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("branch", sa.Text()),
        sa.Column("worktree_path", sa.Text()),
        sa.Column("pr_url", sa.Text()),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("pr_repo", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column("name", sa.Text()),
        sa.Column("parent_task_id", sa.Text()),
        sa.Column("phase", sa.Text(), server_default="execute"),
        sa.Column("deleted_at", sa.Text()),
        sa.Column("user_id", sa.Text()),
    )
    op.execute(
        sa.text(
            "INSERT INTO tasks_old "
            "(id, worker_id, guild_id, description, tool, issue_number, issue_repo, "
            "state, branch, worktree_path, pr_url, pr_number, pr_repo, created_at, "
            "finished_at, name, parent_task_id, phase, deleted_at, user_id) "
            "SELECT t.id, t.worker_id, g.guild_id, t.description, t.tool, "
            "t.issue_number, t.issue_repo, t.state, t.branch, t.worktree_path, "
            "t.pr_url, t.pr_number, t.pr_repo, t.created_at, t.finished_at, "
            "t.name, t.parent_task_id, t.phase, t.deleted_at, t.user_id "
            "FROM tasks t JOIN guilds g ON g.id = t.guild_pk"
        )
    )
    op.drop_table("tasks")
    op.rename_table("tasks_old", "tasks")

    # agents
    op.create_table(
        "agents_old",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
        sa.Column("worker_id", sa.Text(), sa.ForeignKey("workers.id")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False, server_default="worker"),
        sa.Column("state", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("activity", sa.Text()),
        sa.Column("joined_at", sa.Text(), nullable=False),
        sa.Column("last_seen", sa.Text()),
    )
    op.execute(
        sa.text(
            "INSERT INTO agents_old "
            "(id, guild_id, guild_pk, worker_id, name, type, state, activity, joined_at, last_seen) "
            "SELECT a.id, g.guild_id, a.guild_pk, a.worker_id, a.name, a.type, "
            "a.state, a.activity, a.joined_at, a.last_seen "
            "FROM agents a JOIN guilds g ON g.id = a.guild_pk"
        )
    )
    op.drop_table("agents")
    op.rename_table("agents_old", "agents")

    # workers
    op.create_table(
        "workers_old",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("guild_id", sa.Text(), nullable=False),
        sa.Column("guild_pk", sa.Integer(), sa.ForeignKey("guilds.id")),
        sa.Column("repos", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("org", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_seen", sa.Text()),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("auth_token", sa.Text()),
    )
    op.execute(
        sa.text(
            "INSERT INTO workers_old "
            "(id, guild_id, guild_pk, repos, org, state, created_at, last_seen, user_id, auth_token) "
            "SELECT w.id, g.guild_id, w.guild_pk, w.repos, w.org, w.state, "
            "w.created_at, w.last_seen, w.user_id, w.auth_token "
            "FROM workers w JOIN guilds g ON g.id = w.guild_pk"
        )
    )
    op.drop_table("workers")
    op.rename_table("workers_old", "workers")
