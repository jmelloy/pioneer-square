"""Shared test helpers for direct DB manipulation."""

from __future__ import annotations

import contextlib
import os
import secrets
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config as AlembicConfig


def create_db(db_url: str) -> None:
    """Run Alembic migrations to create all tables."""
    alembic_ini = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = AlembicConfig(alembic_ini)
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def truncate_all(db_url: str) -> None:
    """Truncate all application tables and restart sequences."""
    import psycopg2

    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(sync_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        TRUNCATE TABLE
            github_events, foreman_turns, task_logs, guild_keys,
            claude_credentials, guild_members, agents, messages,
            tasks, workers, guilds, github_tokens, user_sessions, users
        RESTART IDENTITY CASCADE
    """)
    conn.close()


@contextlib.contextmanager
def raw_conn(db_url: str):
    """Synchronous psycopg2 connection for direct test DB access.

    Usage (psycopg2 connection for direct test DB access):
        with helpers.raw_conn(db_url) as (conn, cur):
            cur.execute("SELECT ...", params)
            row = cur.fetchone()
            conn.commit()
    """
    import psycopg2
    import psycopg2.extras

    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(sync_url)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def make_auth_token(db_url: str, user_id: str = "gh-user-test", username: str = "testuser") -> str:
    """Insert a test GitHub user + session into the DB and return the token."""
    token = "test-session-" + secrets.token_hex(8)
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "INSERT INTO github_tokens "
            "(github_user_id, github_username, access_token, token_type, scope, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (github_user_id) DO NOTHING",
            (user_id, username, "gh_tok_fake", "bearer", "repo", now, now),
        )
        cur.execute(
            "INSERT INTO user_sessions (token, github_user_id, created_at) VALUES (%s, %s, %s)",
            (token, user_id, now),
        )
    return token


def insert_guild(
    db_url: str,
    guild_id: str,
    name: str = "Test Guild",
    owner_user_id: str | None = "gh-user-test",
) -> None:
    """Insert a guild row directly into the DB.

    By default also inserts ``owner_user_id`` as an ``owner`` member so the
    default ``make_auth_token()`` user satisfies ``require_member`` checks.
    Pass ``owner_user_id=None`` to skip that.
    """
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "INSERT INTO guilds (guild_id, created_at, name, github_user_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (guild_id, now, name, owner_user_id),
        )
        cur.execute(
            "SELECT id FROM guilds WHERE guild_id = %s AND deleted_at IS NULL", (guild_id,)
        )
        row = cur.fetchone()
        guild_pk = row["id"] if row else None
        if owner_user_id and guild_pk:
            cur.execute(
                "INSERT INTO users (id, github_id, github_login, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (owner_user_id, owner_user_id, owner_user_id, now, now),
            )
            cur.execute(
                "INSERT INTO guild_members (guild_pk, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (guild_pk, user_id) DO NOTHING",
                (guild_pk, owner_user_id, "owner", now),
            )


def insert_member(db_url: str, guild_id: str, user_id: str, role: str = "member") -> None:
    """Add a user as a member of a guild (creating a users row if needed)."""
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "INSERT INTO users (id, github_id, github_login, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (user_id, user_id, user_id, now, now),
        )
        cur.execute(
            "SELECT id FROM guilds WHERE guild_id = %s AND deleted_at IS NULL", (guild_id,)
        )
        row = cur.fetchone()
        guild_pk = row["id"] if row else None
        if guild_pk:
            cur.execute(
                "INSERT INTO guild_members (guild_pk, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (guild_pk, user_id) DO UPDATE SET role = EXCLUDED.role",
                (guild_pk, user_id, role, now),
            )
