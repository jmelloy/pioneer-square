"""Tests for the auth gates on /auth/claude/credentials and /auth/github/token.

These endpoints used to be unauthenticated — anyone with a guild_id could
exfiltrate or overwrite stored Claude OAuth credentials and the guild's
GitHub token. The fix introduces a worker auth_token (issued at registration)
plus a member-login fallback. These tests pin that contract.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))
from helpers import _sync_session, insert_guild, make_auth_token
from models import ClaudeCredentials, GithubToken, Guild
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col  # noqa: E402


def _register_worker(test_client, guild_id: str) -> dict:
    resp = test_client.post(
        f"/guilds/{guild_id}/workers",
        json={"repos": ["owner/repo"]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_claude_credentials(db_url: str, guild_id: str, blob: str = "BLOB") -> None:

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(col(Guild.id)).where(
                col(Guild.slug) == guild_id, col(Guild.deleted_at).is_(None)
            )
        )
        stmt = (
            pg_insert(ClaudeCredentials)
            .values(
                guild_id=guild_pk,
                credentials_blob=blob,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["guild_id"],
                set_={
                    "credentials_blob": pg_insert(ClaudeCredentials).excluded.credentials_blob,
                    "updated_at": pg_insert(ClaudeCredentials).excluded.updated_at,
                },
            )
        )
        session.execute(stmt)
        session.commit()


def _seed_github_token(db_url: str, user_id: str = "gh-user-test") -> None:
    """The default test user already has a github_tokens row via make_auth_token —
    this is a no-op that just ensures it's there for clarity."""

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        existing = session.scalar(
            select(col(GithubToken.github_user_id)).where(
                col(GithubToken.github_user_id) == user_id
            )
        )
        if existing:
            return
        stmt = (
            pg_insert(GithubToken)
            .values(
                github_user_id=user_id,
                github_username="tester",
                access_token="gh_tok",
                token_type="bearer",
                scope="repo",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["github_user_id"])
        )
        session.execute(stmt)
        session.commit()


# ---------------------------------------------------------------------------
# /auth/claude/credentials GET
# ---------------------------------------------------------------------------


def test_get_claude_credentials_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-claude")
    _seed_claude_credentials(db_url, "g-claude")
    resp = test_client.get("/auth/claude/credentials", params={"guild_id": "g-claude"})
    assert resp.status_code == 401


def test_get_claude_credentials_rejects_random_token(client):
    test_client, db_url = client
    insert_guild(db_url, "g-claude")
    _seed_claude_credentials(db_url, "g-claude")
    resp = test_client.get(
        "/auth/claude/credentials",
        params={"guild_id": "g-claude"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


def test_get_claude_credentials_accepts_worker_token(client):
    test_client, db_url = client
    insert_guild(db_url, "g-claude")
    _seed_claude_credentials(db_url, "g-claude", blob="THE_BLOB")
    worker = _register_worker(test_client, "g-claude")
    auth_token = worker["auth_token"]
    assert auth_token, "registration response must include auth_token"
    resp = test_client.get(
        "/auth/claude/credentials",
        params={"guild_id": "g-claude"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["credentials_blob"] == "THE_BLOB"


def test_get_claude_credentials_accepts_member_login_token(client):
    test_client, db_url = client
    insert_guild(db_url, "g-claude")
    _seed_claude_credentials(db_url, "g-claude", blob="THE_BLOB")
    login_token = make_auth_token(db_url)
    resp = test_client.get(
        "/auth/claude/credentials",
        params={"guild_id": "g-claude"},
        headers={"Authorization": f"Bearer {login_token}"},
    )
    assert resp.status_code == 200


def test_get_claude_credentials_rejects_worker_token_for_other_guild(client):
    test_client, db_url = client
    insert_guild(db_url, "g-mine")
    insert_guild(db_url, "g-other")
    _seed_claude_credentials(db_url, "g-other", blob="SECRET")
    worker = _register_worker(test_client, "g-mine")
    resp = test_client.get(
        "/auth/claude/credentials",
        params={"guild_id": "g-other"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /auth/claude/credentials POST
# ---------------------------------------------------------------------------


def test_post_claude_credentials_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-write")
    resp = test_client.post(
        "/auth/claude/credentials",
        json={"slug": "g-write", "credentials_blob": "NEW"},
    )
    assert resp.status_code == 401


def test_post_claude_credentials_accepts_worker_token(client):
    test_client, db_url = client
    insert_guild(db_url, "g-write")
    worker = _register_worker(test_client, "g-write")
    resp = test_client.post(
        "/auth/claude/credentials",
        json={"slug": "g-write", "credentials_blob": "NEW"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200

    with _sync_session(db_url) as session:
        blob = session.scalar(
            select(col(ClaudeCredentials.credentials_blob))
            .join(Guild, col(Guild.id) == ClaudeCredentials.guild_id)
            .where(col(Guild.slug) == "g-write", col(Guild.deleted_at).is_(None))
        )
    assert blob == "NEW"


# ---------------------------------------------------------------------------
# /auth/github/token GET
# ---------------------------------------------------------------------------


def test_get_github_token_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-gh")
    _seed_github_token(db_url)
    resp = test_client.get("/auth/github/token", params={"guild_id": "g-gh"})
    assert resp.status_code == 401


def test_get_github_token_accepts_worker_token(client):
    test_client, db_url = client
    insert_guild(db_url, "g-gh")
    _seed_github_token(db_url)
    worker = _register_worker(test_client, "g-gh")
    resp = test_client.get(
        "/auth/github/token",
        params={"guild_id": "g-gh"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_get_github_token_uses_guild_members_owner(client):
    """Token endpoint resolves the owner via guild_members, not guilds.github_user_id."""
    test_client, db_url = client
    # insert_guild adds owner "gh-user-test" to guild_members; make_auth_token creates their token
    insert_guild(db_url, "g-gm-tok")
    _seed_github_token(db_url)  # ensures gh-user-test has a github_tokens row
    worker = _register_worker(test_client, "g-gm-tok")
    resp = test_client.get(
        "/auth/github/token",
        params={"guild_id": "g-gm-tok"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "username" in data


def test_get_github_token_no_owner_in_guild_members(client):
    """Returns 404 when no owner exists in guild_members (legacy-only guild)."""
    from datetime import UTC, datetime

    test_client, db_url = client
    now = datetime.now(UTC)
    # Insert a guild with github_user_id but no guild_members row
    with _sync_session(db_url) as session:
        stmt = (
            pg_insert(Guild)
            .values(
                slug="g-noowner",
                created_at=now,
                name="No Owner",
                github_user_id="gh-user-test",
            )
            .on_conflict_do_nothing()
        )
        session.execute(stmt)
        session.commit()
    # Register a worker (bypasses membership check via auth_token)
    worker = _register_worker(test_client, "g-noowner")
    resp = test_client.get(
        "/auth/github/token",
        params={"guild_id": "g-noowner"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 404
