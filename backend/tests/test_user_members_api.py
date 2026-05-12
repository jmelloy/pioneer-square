"""Tests for /api/me and the guild member management endpoints."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import insert_guild, insert_member, make_auth_token


def _insert_guild_legacy_only(db_path: str, guild_id: str, user_id: str) -> None:
    """Insert a guild with github_user_id but no guild_members row."""
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO guilds (guild_id, created_at, name, github_user_id) VALUES (?, ?, ?, ?)",
            (guild_id, now, "Legacy", user_id),
        )
        conn.commit()


def _seed_user(db_path: str, user_id: str, login: str) -> None:
    """Insert a users row directly so it can be referenced as a member."""
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users "
            "(id, github_id, github_login, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, user_id, login, now, now),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# /api/me
# ---------------------------------------------------------------------------


def test_api_me_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/api/me")
    assert resp.status_code == 401


def test_api_me_returns_profile_and_memberships(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-1", name="My Guild")
    _seed_user(db_path, "gh-user-test", "testuser")
    token = make_auth_token(db_path)
    resp = test_client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["github_login"] == "testuser"
    guild_ids = [m["guild_id"] for m in data["memberships"]]
    assert "gm-1" in guild_ids
    membership = next(m for m in data["memberships"] if m["guild_id"] == "gm-1")
    assert membership["role"] == "owner"


# ---------------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------------


def test_list_members_requires_membership(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-list")
    other = make_auth_token(db_path, user_id="other-user", username="outsider")
    resp = test_client.get(
        "/api/guilds/gm-list/members",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 403


def test_list_members_returns_owner(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-list2")
    token = make_auth_token(db_path)
    resp = test_client.get(
        "/api/guilds/gm-list2/members",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    members = resp.json()
    assert len(members) == 1
    assert members[0]["user_id"] == "gh-user-test"
    assert members[0]["role"] == "owner"


def test_add_member_resolves_login(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-add")
    _seed_user(db_path, "user-99", "octocat")
    token = make_auth_token(db_path)
    resp = test_client.post(
        "/api/guilds/gm-add/members",
        json={"user": "octocat", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "user-99"
    assert body["role"] == "member"


def test_add_member_unknown_user_is_404(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-unknown")
    token = make_auth_token(db_path)
    resp = test_client.post(
        "/api/guilds/gm-unknown/members",
        json={"user": "noone-here", "role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_add_member_requires_owner(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-perm")
    insert_member(db_path, "gm-perm", "user-bob", role="member")
    bob_token = make_auth_token(db_path, user_id="user-bob", username="bob")
    _seed_user(db_path, "user-99", "octocat")
    resp = test_client.post(
        "/api/guilds/gm-perm/members",
        json={"user": "octocat", "role": "member"},
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resp.status_code == 403


def test_update_member_role(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-role")
    insert_member(db_path, "gm-role", "user-bob", role="member")
    token = make_auth_token(db_path)
    resp = test_client.patch(
        "/api/guilds/gm-role/members/user-bob",
        json={"role": "viewer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "viewer"


def test_cannot_demote_last_owner(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-last")
    token = make_auth_token(db_path)
    resp = test_client.patch(
        "/api/guilds/gm-last/members/gh-user-test",
        json={"role": "member"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_remove_member(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-del")
    insert_member(db_path, "gm-del", "user-bob", role="member")
    token = make_auth_token(db_path)
    resp = test_client.delete(
        "/api/guilds/gm-del/members/user-bob",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_invalid_role_rejected(client):
    test_client, db_path = client
    insert_guild(db_path, "gm-bad")
    _seed_user(db_path, "user-99", "octocat")
    token = make_auth_token(db_path)
    resp = test_client.post(
        "/api/guilds/gm-bad/members",
        json={"user": "octocat", "role": "admin"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# guild_members migration — /api/me must not include legacy guilds
# ---------------------------------------------------------------------------


def test_api_me_excludes_legacy_guilds(client):
    """Guilds where guilds.github_user_id matches but no guild_members row exists
    must NOT appear in /api/me memberships; the legacy path has been removed."""
    test_client, db_path = client
    _seed_user(db_path, "gh-user-test", "testuser")
    _insert_guild_legacy_only(db_path, "gm-legacy", "gh-user-test")
    token = make_auth_token(db_path)
    resp = test_client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    guild_ids = [m["guild_id"] for m in resp.json()["memberships"]]
    assert "gm-legacy" not in guild_ids
