"""Tests for foreman context REST endpoints (per-guild+user scoping)."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import _sync_session, insert_guild, insert_member, insert_task, make_auth_token
from models import ForemanTurn, Guild
from sqlalchemy import select
from sqlmodel import col  # noqa: E402


def _insert_foreman_turn(db_url: str, guild_id: str, user_id: str, role: str, content: str) -> None:

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(col(Guild.id)).where(
                col(Guild.guild_id) == guild_id, col(Guild.deleted_at).is_(None)
            )
        )
        session.add(
            ForemanTurn(
                guild_id=guild_pk or 0,
                user_id=user_id,
                role=role,
                content_json=f'"{content}"',
                is_tool_response=0,
                created_at=now,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Authentication enforcement
# ---------------------------------------------------------------------------


def test_get_foreman_context_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-auth")
    resp = test_client.get("/guilds/g-auth/foreman/context")
    assert resp.status_code == 401


def test_clear_foreman_context_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-auth2")
    resp = test_client.post("/guilds/g-auth2/foreman/clear-context")
    assert resp.status_code == 401


def test_get_foreman_context_guild_not_found(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    resp = test_client.get(
        "/guilds/doesnotexist/foreman/context",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_clear_foreman_context_guild_not_found(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/guilds/doesnotexist/foreman/clear-context",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Per-user scoping
# ---------------------------------------------------------------------------


def test_get_foreman_context_returns_only_requesting_users_turns(client):
    """Each user only sees their own foreman turns, not other users'."""
    test_client, db_url = client
    insert_guild(db_url, "g-scope")
    token_a = make_auth_token(db_url, user_id="user-alice", username="alice")
    token_b = make_auth_token(db_url, user_id="user-bob", username="bob")
    insert_member(db_url, "g-scope", "user-alice", role="member")
    insert_member(db_url, "g-scope", "user-bob", role="member")

    _insert_foreman_turn(db_url, "g-scope", "user-alice", "user", "Hello from Alice")
    _insert_foreman_turn(db_url, "g-scope", "user-bob", "user", "Hello from Bob")

    resp_a = test_client.get(
        "/guilds/g-scope/foreman/context",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["count"] == 1
    assert data_a["messages"][0]["content"] == "Hello from Alice"

    resp_b = test_client.get(
        "/guilds/g-scope/foreman/context",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["count"] == 1
    assert data_b["messages"][0]["content"] == "Hello from Bob"


def test_get_foreman_context_empty_for_new_user(client):
    """A user with no conversation turns gets an empty list."""
    test_client, db_url = client
    insert_guild(db_url, "g-empty")
    token = make_auth_token(db_url)

    resp = test_client.get(
        "/guilds/g-empty/foreman/context",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["messages"] == []


def test_clear_foreman_context_only_clears_requesting_users_turns(client):
    """Clearing context only removes the requesting user's turns."""
    test_client, db_url = client
    insert_guild(db_url, "g-clear")
    token_a = make_auth_token(db_url, user_id="user-alice2", username="alice2")
    token_b = make_auth_token(db_url, user_id="user-bob2", username="bob2")
    insert_member(db_url, "g-clear", "user-alice2", role="member")
    insert_member(db_url, "g-clear", "user-bob2", role="member")

    _insert_foreman_turn(db_url, "g-clear", "user-alice2", "user", "Alice message")
    _insert_foreman_turn(db_url, "g-clear", "user-bob2", "user", "Bob message")

    # Alice clears her context
    resp = test_client.post(
        "/guilds/g-clear/foreman/clear-context",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1

    # Alice's turns are gone
    resp_a = test_client.get(
        "/guilds/g-clear/foreman/context",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp_a.json()["count"] == 0

    # Bob's turns are untouched
    resp_b = test_client.get(
        "/guilds/g-clear/foreman/context",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b.json()["count"] == 1
    assert resp_b.json()["messages"][0]["content"] == "Bob message"


def test_get_foreman_context_isolates_across_guilds(client):
    """Turns from a different guild are not returned even for the same user."""
    test_client, db_url = client
    insert_guild(db_url, "g-iso1")
    insert_guild(db_url, "g-iso2")
    token = make_auth_token(db_url)

    _insert_foreman_turn(db_url, "g-iso1", "gh-user-test", "user", "Guild 1 message")

    resp = test_client.get(
        "/guilds/g-iso2/foreman/context",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


# ---------------------------------------------------------------------------
# task_id propagation
# ---------------------------------------------------------------------------


def test_create_foreman_turn_persists_task_id(client):
    """POST /guilds/{guild_id}/foreman/history with task_id stores it on the row."""
    test_client, db_url = client
    insert_guild(db_url, "g-ftask")
    token = make_auth_token(db_url)
    insert_task(db_url, "g-ftask", "t-ftask1")

    resp = test_client.post(
        "/guilds/g-ftask/foreman/history",
        json={
            "user_id": "gh-user-test",
            "role": "user",
            "content_json": '"Hello"',
            "task_id": "t-ftask1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    turn_id = resp.json()["id"]

    with _sync_session(db_url) as session:
        turn = session.scalar(select(ForemanTurn).where(col(ForemanTurn.id) == turn_id))
    assert turn is not None
    assert turn.task_id == "t-ftask1"


def test_create_foreman_turn_null_task_id_by_default(client):
    """POST /guilds/{guild_id}/foreman/history without task_id stores NULL."""
    test_client, db_url = client
    insert_guild(db_url, "g-ftask-null")
    token = make_auth_token(db_url)

    resp = test_client.post(
        "/guilds/g-ftask-null/foreman/history",
        json={
            "user_id": "gh-user-test",
            "role": "user",
            "content_json": '"Hello"',
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    turn_id = resp.json()["id"]

    with _sync_session(db_url) as session:
        turn = session.scalar(select(ForemanTurn).where(col(ForemanTurn.id) == turn_id))
    assert turn is not None
    assert turn.task_id is None


def test_create_foreman_turn_cross_guild_task_returns_404(client):
    """task_id that belongs to a different guild must return 404."""
    test_client, db_url = client
    insert_guild(db_url, "g-xguild-a")
    insert_guild(db_url, "g-xguild-b")
    token = make_auth_token(db_url)
    insert_task(db_url, "g-xguild-a", "t-xguild1")

    # Reference guild-A's task from guild-B's endpoint — must be rejected.
    resp = test_client.post(
        "/guilds/g-xguild-b/foreman/history",
        json={
            "user_id": "gh-user-test",
            "role": "user",
            "content_json": '"Hello"',
            "task_id": "t-xguild1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
