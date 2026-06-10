#!/usr/bin/env python3
"""Idempotent SQLite → PostgreSQL data migration for Pioneer Square.

Supports SQLite databases at any schema version — old-style (guilds.id TEXT)
and new-style (guilds.id INTEGER + guilds.guild_id TEXT) are both handled.
Rows that already exist in PostgreSQL (same PK) are silently skipped.

Usage:
    python scripts/migrate_sqlite_to_postgres.py \\
        --sqlite-path /path/to/pioneer_square.db \\
        --postgres-url postgresql://pioneer:pioneer_password@localhost/pioneer_square

Environment variables (used when the corresponding flag is omitted):
    SQLITE_PATH   path to the SQLite database file
    DATABASE_URL  PostgreSQL URL (postgresql+asyncpg://... scheme is accepted)

Run `alembic upgrade head` in the backend before executing this script so
that the target PostgreSQL schema is fully up-to-date.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any

try:
    import psycopg2
except ImportError:
    print("psycopg2 is required: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pg_url(url: str) -> str:
    """Strip asyncpg scheme prefix so psycopg2 can parse the URL."""
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _row(cols: list[str], values: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(cols, values, strict=False))


def _log(table: str, inserted: int, skipped: int) -> None:
    print(f"  {table:30s}  {inserted:6d} inserted  {skipped:6d} skipped")


def _reset_seq(pg: Any, table: str, col: str = "id") -> None:
    """Advance a SERIAL sequence past the highest existing id."""
    with pg.cursor() as cur:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
            f"GREATEST(1, (SELECT COALESCE(MAX({col}), 0) FROM {table})))"
        )
    pg.commit()


# ---------------------------------------------------------------------------
# Per-table migrators
# ---------------------------------------------------------------------------


def migrate_github_tokens(sq: sqlite3.Connection, pg: Any) -> None:
    if not _exists(sq, "github_tokens"):
        return
    cols = _columns(sq, "github_tokens")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM github_tokens"):
            d = _row(cols, raw)
            cur.execute(
                """
                INSERT INTO github_tokens
                    (github_user_id, github_username, access_token,
                     token_type, scope, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (github_user_id) DO NOTHING
                """,
                (
                    d.get("github_user_id"),
                    d.get("github_username"),
                    d.get("access_token"),
                    d.get("token_type", "bearer"),
                    d.get("scope"),
                    d.get("created_at"),
                    d.get("updated_at", d.get("created_at")),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("github_tokens", inserted, skipped)


def migrate_users(sq: sqlite3.Connection, pg: Any) -> None:
    if not _exists(sq, "users"):
        return
    cols = _columns(sq, "users")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM users"):
            d = _row(cols, raw)
            cur.execute(
                """
                INSERT INTO users
                    (id, github_id, github_login, email, display_name,
                     avatar_url, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    d.get("github_id"),
                    d.get("github_login"),
                    d.get("email"),
                    d.get("display_name"),
                    d.get("avatar_url"),
                    d.get("created_at"),
                    d.get("updated_at", d.get("created_at")),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("users", inserted, skipped)


def migrate_user_sessions(sq: sqlite3.Connection, pg: Any) -> None:
    if not _exists(sq, "user_sessions"):
        return
    cols = _columns(sq, "user_sessions")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM user_sessions"):
            d = _row(cols, raw)
            cur.execute(
                """
                INSERT INTO user_sessions (token, github_user_id, created_at)
                VALUES (%s,%s,%s)
                ON CONFLICT (token) DO NOTHING
                """,
                (d.get("token"), d.get("github_user_id"), d.get("created_at")),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("user_sessions", inserted, skipped)


def migrate_guilds(sq: sqlite3.Connection, pg: Any) -> dict[str, int]:
    """Migrate guilds and return a mapping: old TEXT guild_id → PG integer id."""
    if not _exists(sq, "guilds"):
        return {}

    cols = _columns(sq, "guilds")
    # Old SQLite schema: guilds.id is the TEXT guild_id itself.
    # New SQLite schema (after 20260512_000000): guilds.id is INTEGER, guilds.guild_id is TEXT.
    old_schema = "guild_id" not in cols

    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM guilds"):
            d = _row(cols, raw)
            guild_id = d["id"] if old_schema else d["guild_id"]
            cur.execute(
                """
                INSERT INTO guilds
                    (guild_id, created_at, name, github_user_id, primary_repo,
                     webhook_secret, description, url, version, deleted_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (
                    guild_id,
                    d.get("created_at"),
                    d.get("name"),
                    d.get("github_user_id"),
                    d.get("primary_repo"),
                    d.get("webhook_secret"),
                    d.get("description"),
                    d.get("url"),
                    d.get("version"),
                    d.get("deleted_at"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("guilds", inserted, skipped)

    with pg.cursor() as cur:
        cur.execute("SELECT id, guild_id FROM guilds")
        return {gid: pg_id for pg_id, gid in cur.fetchall()}


def _lookup_guild_pk(
    d: dict[str, Any],
    guild_map: dict[str, int],
    sq_int_map: dict[int, int] | None = None,
) -> int | None:
    # New SQLite schema: tables carry guild_pk (INTEGER FK to guilds.id).
    # IDs match between SQLite and PG since guilds were migrated in the same order.
    sq_pk = d.get("guild_pk")
    if sq_pk is not None:
        return sq_pk
    # Old SQLite schema: tables carry guild_id (TEXT).
    guild_id = d.get("guild_id")
    if guild_id is None:
        return None
    return guild_map.get(guild_id)


def migrate_workers(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "workers"):
        return
    cols = _columns(sq, "workers")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM workers"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    workers: skipping {d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO workers
                    (id, guild_pk, repos, org, state, created_at,
                     last_seen, user_id, auth_token, name)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    guild_pk,
                    d.get("repos", "[]"),
                    d.get("org"),
                    d.get("state", "idle"),
                    d.get("created_at"),
                    d.get("last_seen"),
                    d.get("user_id"),
                    d.get("auth_token"),
                    d.get("name"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("workers", inserted, skipped)


def migrate_agents(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "agents"):
        return
    cols = _columns(sq, "agents")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM agents"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    agents: skipping {d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO agents
                    (id, guild_pk, worker_id, name, type, state, activity,
                     current_task_id, joined_at, last_seen)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    guild_pk,
                    d.get("worker_id"),
                    d.get("name"),
                    d.get("type", "worker"),
                    d.get("state", "idle"),
                    d.get("activity"),
                    d.get("current_task_id"),
                    d.get("joined_at"),
                    d.get("last_seen"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("agents", inserted, skipped)


def migrate_tasks(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "tasks"):
        return
    cols = _columns(sq, "tasks")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM tasks"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    tasks: skipping {d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO tasks
                    (id, worker_id, guild_pk, description, tool,
                     issue_number, issue_repo, state, branch, worktree_path,
                     pr_url, pr_number, pr_repo, created_at, finished_at,
                     name, parent_task_id, phase, finalized_at, user_id,
                     locked_at, lock_holder)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    d.get("worker_id"),
                    guild_pk,
                    d.get("description"),
                    d.get("tool", "claude"),
                    d.get("issue_number"),
                    d.get("issue_repo"),
                    d.get("state", "pending"),
                    d.get("branch"),
                    d.get("worktree_path"),
                    d.get("pr_url"),
                    d.get("pr_number"),
                    d.get("pr_repo"),
                    d.get("created_at"),
                    d.get("finished_at"),
                    d.get("name"),
                    d.get("parent_task_id"),
                    d.get("phase", "execute"),
                    d.get("finalized_at"),
                    d.get("user_id"),
                    d.get("locked_at"),
                    d.get("lock_holder"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("tasks", inserted, skipped)


def migrate_task_logs(sq: sqlite3.Connection, pg: Any) -> None:
    if not _exists(sq, "task_logs"):
        return
    cols = _columns(sq, "task_logs")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM task_logs ORDER BY id"):
            d = _row(cols, raw)
            cur.execute(
                """
                INSERT INTO task_logs
                    (id, task_id, timestamp, line, worker_id, agent_id, data)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    d.get("task_id"),
                    d.get("timestamp"),
                    d.get("line"),
                    d.get("worker_id"),
                    d.get("agent_id"),
                    d.get("data"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _reset_seq(pg, "task_logs")
    _log("task_logs", inserted, skipped)


def migrate_messages(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "messages"):
        return
    cols = _columns(sq, "messages")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM messages ORDER BY id"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    messages: skipping id={d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO messages
                    (id, guild_pk, from_agent, to_agent, content,
                     message_type, created_at, user_id, role, meta)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    guild_pk,
                    d.get("from_agent"),
                    d.get("to_agent"),
                    d.get("content"),
                    d.get("message_type"),
                    d.get("created_at"),
                    d.get("user_id"),
                    d.get("role"),
                    d.get("meta"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _reset_seq(pg, "messages")
    _log("messages", inserted, skipped)


def migrate_guild_members(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "guild_members"):
        return
    cols = _columns(sq, "guild_members")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM guild_members"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    guild_members: skipping user={d.get('user_id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO guild_members (guild_pk, user_id, role, created_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (guild_pk, user_id) DO NOTHING
                """,
                (guild_pk, d.get("user_id"), d.get("role", "member"), d.get("created_at")),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _log("guild_members", inserted, skipped)


def migrate_claude_credentials(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "claude_credentials"):
        return
    cols = _columns(sq, "claude_credentials")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM claude_credentials ORDER BY id"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    claude_credentials: skipping id={d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO claude_credentials
                    (id, guild_pk, credentials_blob, updated_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (d.get("id"), guild_pk, d.get("credentials_blob"), d.get("updated_at")),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _reset_seq(pg, "claude_credentials")
    _log("claude_credentials", inserted, skipped)


def migrate_guild_keys(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "guild_keys"):
        return
    cols = _columns(sq, "guild_keys")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM guild_keys ORDER BY id"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    guild_keys: skipping id={d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO guild_keys
                    (id, guild_pk, key_id, public_key_pem, private_key_pem,
                     created_at, custom_jwks, private_key_jwk)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    guild_pk,
                    d.get("key_id"),
                    d.get("public_key_pem"),
                    d.get("private_key_pem"),
                    d.get("created_at"),
                    d.get("custom_jwks"),
                    d.get("private_key_jwk"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _reset_seq(pg, "guild_keys")
    _log("guild_keys", inserted, skipped)


def migrate_github_events(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "github_events"):
        return
    cols = _columns(sq, "github_events")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM github_events ORDER BY id"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    github_events: skipping id={d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO github_events
                    (id, guild_pk, task_id, delivery_id, event_type, action,
                     repo, pr_number, pr_url, sender_login, payload_json, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    guild_pk,
                    d.get("task_id"),
                    d.get("delivery_id"),
                    d.get("event_type"),
                    d.get("action"),
                    d.get("repo"),
                    d.get("pr_number"),
                    d.get("pr_url"),
                    d.get("sender_login"),
                    d.get("payload_json"),
                    d.get("created_at"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _reset_seq(pg, "github_events")
    _log("github_events", inserted, skipped)


def migrate_foreman_turns(sq: sqlite3.Connection, pg: Any, guild_map: dict[str, int]) -> None:
    if not _exists(sq, "foreman_turns"):
        return
    cols = _columns(sq, "foreman_turns")
    inserted = skipped = 0
    with pg.cursor() as cur:
        for raw in sq.execute("SELECT * FROM foreman_turns ORDER BY id"):
            d = _row(cols, raw)
            guild_pk = _lookup_guild_pk(d, guild_map)
            if guild_pk is None:
                print(f"    foreman_turns: skipping id={d.get('id')} — guild not in map")
                continue
            cur.execute(
                """
                INSERT INTO foreman_turns
                    (id, guild_pk, user_id, role, content_json,
                     is_tool_response, parent_id, created_at,
                     input_tokens, output_tokens)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    d.get("id"),
                    guild_pk,
                    d.get("user_id"),
                    d.get("role"),
                    d.get("content_json"),
                    d.get("is_tool_response", 0),
                    d.get("parent_id"),
                    d.get("created_at"),
                    d.get("input_tokens"),
                    d.get("output_tokens"),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    pg.commit()
    _reset_seq(pg, "foreman_turns")
    _log("foreman_turns", inserted, skipped)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--sqlite-path",
        default=os.environ.get("SQLITE_PATH"),
        help="Path to the SQLite database file (or set SQLITE_PATH)",
    )
    parser.add_argument(
        "--postgres-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL connection URL (or set DATABASE_URL)",
    )
    args = parser.parse_args()

    if not args.sqlite_path:
        parser.error("--sqlite-path (or SQLITE_PATH env var) is required")
    if not args.postgres_url:
        parser.error("--postgres-url (or DATABASE_URL env var) is required")

    sqlite_path = args.sqlite_path
    pg_url = _pg_url(args.postgres_url)

    print(f"Source: {sqlite_path}")
    print(f"Target: {pg_url}")
    print()

    sq = sqlite3.connect(sqlite_path)
    sq.row_factory = None  # keep tuples; we zip with column names ourselves

    try:
        pg = psycopg2.connect(pg_url)
    except psycopg2.OperationalError as exc:
        print(f"Cannot connect to PostgreSQL: {exc}", file=sys.stderr)
        return 1

    print("Migrating tables...")
    migrate_github_tokens(sq, pg)
    migrate_users(sq, pg)
    migrate_user_sessions(sq, pg)
    guild_map = migrate_guilds(sq, pg)
    migrate_workers(sq, pg, guild_map)
    migrate_agents(sq, pg, guild_map)
    migrate_tasks(sq, pg, guild_map)
    migrate_task_logs(sq, pg)
    migrate_messages(sq, pg, guild_map)
    migrate_guild_members(sq, pg, guild_map)
    migrate_claude_credentials(sq, pg, guild_map)
    migrate_guild_keys(sq, pg, guild_map)
    migrate_github_events(sq, pg, guild_map)
    migrate_foreman_turns(sq, pg, guild_map)

    sq.close()
    pg.close()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
