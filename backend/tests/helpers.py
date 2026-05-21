"""Shared test helpers for direct DB manipulation."""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from datetime import UTC, datetime

# Make backend models importable (helpers.py is loaded before conftest.py
# sets up sys.path, so we need to add the backend dir ourselves).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alembic import command
from alembic.config import Config as AlembicConfig


def _psycopg2_kwargs(db_url: str) -> dict:
    """Parse a SQLAlchemy URL and return kwargs suitable for psycopg2.connect()."""
    from sqlalchemy.engine.url import make_url

    u = make_url(db_url).set(drivername="postgresql+psycopg2")
    return {
        "host": u.host,
        "port": u.port or 5432,
        "user": u.username,
        "password": u.password,
        "dbname": u.database,
    }


def create_db(db_url: str) -> None:
    """Run Alembic migrations to create all tables."""
    alembic_ini = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = AlembicConfig(alembic_ini)
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def truncate_all(db_url: str) -> None:
    """Truncate all public tables (except alembic_version) and restart sequences."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(**_psycopg2_kwargs(db_url))
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename != 'alembic_version'"
        )
        tables = [row["tablename"] for row in cur.fetchall()]
        if tables:
            cur.execute(
                "TRUNCATE TABLE {} RESTART IDENTITY CASCADE".format(
                    ", ".join(f'"{t}"' for t in tables)
                )
            )
    finally:
        cur.close()
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

    conn = psycopg2.connect(**_psycopg2_kwargs(db_url))
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


@contextlib.contextmanager
def _sync_session(db_url: str):
    """Synchronous SQLAlchemy ORM session for test DB access."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine.url import make_url
    from sqlalchemy.orm import Session

    sync_url = make_url(db_url).set(drivername="postgresql+psycopg2")
    engine = create_engine(sync_url)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        engine.dispose()


def make_auth_token(db_url: str, user_id: str = "gh-user-test", username: str = "testuser") -> str:
    """Insert a test GitHub user + session into the DB and return the token."""
    from models import GithubToken, UserSession
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    token = "test-session-" + secrets.token_hex(8)
    now = datetime.now(UTC).isoformat()
    with _sync_session(db_url) as session:
        stmt = (
            pg_insert(GithubToken)
            .values(
                github_user_id=user_id,
                github_username=username,
                access_token="gh_tok_fake",
                token_type="bearer",
                scope="repo",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["github_user_id"])
        )
        session.execute(stmt)
        session.add(UserSession(token=token, github_user_id=user_id, created_at=now))
        session.commit()
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
    from models import Guild, GuildMember, User
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now(UTC).isoformat()
    with _sync_session(db_url) as session:
        stmt = (
            pg_insert(Guild)
            .values(guild_id=guild_id, created_at=now, name=name, github_user_id=owner_user_id)
            .on_conflict_do_update(
                index_elements=["guild_id"],
                index_where=Guild.deleted_at.is_(None),
                set_={"name": pg_insert(Guild).excluded.name},
            )
            .returning(Guild.id)
        )
        result = session.execute(stmt)
        guild_pk = result.scalar_one()

        if owner_user_id:
            user_stmt = (
                pg_insert(User)
                .values(
                    id=owner_user_id,
                    github_id=owner_user_id,
                    github_login=owner_user_id,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            session.execute(user_stmt)

            member_stmt = (
                pg_insert(GuildMember)
                .values(
                    guild_id=guild_pk,
                    user_id=owner_user_id,
                    role="owner",
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["guild_id", "user_id"])
            )
            session.execute(member_stmt)

        session.commit()


def insert_member(db_url: str, guild_id: str, user_id: str, role: str = "member") -> None:
    """Add a user as a member of a guild (creating a users row if needed)."""
    from models import Guild, GuildMember, User
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now(UTC).isoformat()
    with _sync_session(db_url) as session:
        user_stmt = (
            pg_insert(User)
            .values(
                id=user_id,
                github_id=user_id,
                github_login=user_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.execute(user_stmt)

        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        if guild_pk:
            member_stmt = (
                pg_insert(GuildMember)
                .values(
                    guild_id=guild_pk,
                    user_id=user_id,
                    role=role,
                    created_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["guild_id", "user_id"],
                    set_={"role": pg_insert(GuildMember).excluded.role},
                )
            )
            session.execute(member_stmt)

        session.commit()


def insert_worker(
    db_url: str,
    guild_id: str,
    worker_id: str,
    state: str = "online",
    repos: str = "[]",
    org: str | None = None,
) -> None:
    """Insert a worker row for a guild."""
    from models import Guild, Worker
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now(UTC).isoformat()
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        assert guild_pk is not None, f"Guild {guild_id!r} not found"
        stmt = (
            pg_insert(Worker)
            .values(
                id=worker_id,
                guild_id=guild_pk,
                repos=repos,
                state=state,
                org=org,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.execute(stmt)
        session.commit()


def insert_agent(
    db_url: str,
    guild_id: str,
    agent_id: str,
    *,
    worker_id: str | None = None,
    state: str = "idle",
    agent_type: str = "worker",
    last_seen: str | None = None,
    current_task_id: str | None = None,
) -> None:
    """Insert an agent row for a guild."""
    from models import Agent, Guild
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now(UTC).isoformat()
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        assert guild_pk is not None, f"Guild {guild_id!r} not found"
        stmt = (
            pg_insert(Agent)
            .values(
                id=agent_id,
                guild_id=guild_pk,
                worker_id=worker_id,
                name=agent_id.upper(),
                type=agent_type,
                state=state,
                joined_at=now,
                last_seen=last_seen,
                current_task_id=current_task_id,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.execute(stmt)
        session.commit()


def insert_task(
    db_url: str,
    guild_id: str,
    task_id: str,
    *,
    worker_id: str | None = None,
    description: str = "Test task",
    state: str = "pending",
    phase: str = "execute",
    tool: str = "claude",
    branch: str | None = None,
    pr_url: str | None = None,
    pr_number: int | None = None,
    pr_repo: str | None = None,
    issue_number: int | None = None,
    issue_repo: str | None = None,
    user_id: str | None = None,
) -> None:
    """Insert a task row for a guild."""
    from models import Guild, Task
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now(UTC).isoformat()
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(Guild.id).where(Guild.guild_id == guild_id, Guild.deleted_at.is_(None))
        )
        assert guild_pk is not None, f"Guild {guild_id!r} not found"
        stmt = (
            pg_insert(Task)
            .values(
                id=task_id,
                worker_id=worker_id,
                guild_id=guild_pk,
                description=description,
                tool=tool,
                state=state,
                phase=phase,
                branch=branch,
                pr_url=pr_url,
                pr_number=pr_number,
                pr_repo=pr_repo,
                issue_number=issue_number,
                issue_repo=issue_repo,
                user_id=user_id,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.execute(stmt)
        session.commit()
