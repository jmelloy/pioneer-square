"""Tests for the auth gate on /auth/github/token.

This endpoint used to be unauthenticated — anyone with a guild_id could
exfiltrate the guild's GitHub token. The fix introduces a worker auth_token
(issued at registration) plus a member-login fallback. These tests pin that
contract.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))
from helpers import _sync_session, insert_guild
from models import GithubToken, Guild, Task
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


def _insert_task(db_url: str, guild_slug: str, task_id: str, user_id: str | None) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == guild_slug))
        assert guild_pk is not None, f"Guild {guild_slug!r} not found"
        session.execute(
            pg_insert(Task)
            .values(
                id=task_id,
                guild_id=guild_pk,
                description="do a thing",
                state="pending",
                created_at=now,
                user_id=user_id,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()


def test_get_github_token_task_id_returns_triggering_user_token(client):
    """With task_id, the endpoint returns the triggering user's own OAuth token
    (push/PR attributed to them), not the guild owner's."""
    test_client, db_url = client
    insert_guild(db_url, "g-task-tok")  # owner gh-user-test, token gh_tok
    # A different human triggered the task; seed their distinct token.
    _seed_github_token(db_url, user_id="gh-triggerer")
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(GithubToken)
            .values(
                github_user_id="gh-triggerer",
                github_username="triggerer",
                access_token="trigger_tok",
                token_type="bearer",
                scope="repo",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            .on_conflict_do_update(
                index_elements=["github_user_id"], set_={"access_token": "trigger_tok"}
            )
        )
        session.commit()
    _insert_task(db_url, "g-task-tok", "t-trig1", user_id="gh-triggerer")
    worker = _register_worker(test_client, "g-task-tok")
    resp = test_client.get(
        "/auth/github/token",
        params={"guild_id": "g-task-tok", "task_id": "t-trig1"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] == "trigger_tok"


def test_get_github_token_task_id_falls_back_when_no_user_token(client):
    """A task whose user has no stored token falls back to the guild owner's
    token (which, in prod with an App configured, would be the App token)."""
    test_client, db_url = client
    insert_guild(db_url, "g-task-fb")
    _seed_github_token(db_url)
    _insert_task(db_url, "g-task-fb", "t-nouser", user_id=None)
    worker = _register_worker(test_client, "g-task-fb")
    resp = test_client.get(
        "/auth/github/token",
        params={"guild_id": "g-task-fb", "task_id": "t-nouser"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200, resp.text
    # Owner gh-user-test's token from _seed_github_token.
    assert resp.json()["access_token"] == "gh_tok"


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
