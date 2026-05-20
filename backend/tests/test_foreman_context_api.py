"""Tests for foreman context REST endpoints (per-guild+user scoping)."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import insert_guild, insert_member, make_auth_token, raw_conn


def _insert_foreman_turn(db_url: str, guild_id: str, user_id: str, role: str, content: str) -> None:
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute("SELECT id FROM guilds WHERE guild_id = %s AND deleted_at IS NULL", (guild_id,))
        row = cur.fetchone()
        guild_pk = row["id"]
        cur.execute(
            "INSERT INTO foreman_turns (guild_pk, user_id, role, content_json, is_tool_response, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (guild_pk, user_id, role, f'"{content}"', False, now),
        )


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
