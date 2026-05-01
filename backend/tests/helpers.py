"""Shared test helpers for direct DB manipulation."""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine


def create_db(db_path: str) -> None:
    """Run Alembic migrations synchronously to create all tables in *db_path*."""
    alembic_ini = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg = AlembicConfig(alembic_ini)
    sync_url = f"sqlite:///{db_path}"
    cfg.set_main_option("sqlalchemy.url", sync_url)
    engine = create_engine(sync_url)
    try:
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()


def make_auth_token(db_path: str, user_id: str = "gh-user-test", username: str = "testuser") -> str:
    """Insert a test GitHub user + session into *db_path* and return the token."""
    token = "test-session-" + secrets.token_hex(8)
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO github_tokens "
            "(github_user_id, github_username, access_token, token_type, scope, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, "gh_tok_fake", "bearer", "repo", now, now),
        )
        conn.execute(
            "INSERT INTO user_sessions (token, github_user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, now),
        )
        conn.commit()
    return token


def insert_guild(db_path: str, guild_id: str, name: str = "Test Guild") -> None:
    """Insert a guild row directly into *db_path*."""
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO guilds (id, created_at, name) VALUES (?, ?, ?)",
            (guild_id, now, name),
        )
        conn.commit()
