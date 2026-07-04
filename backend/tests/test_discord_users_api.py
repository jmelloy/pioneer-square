"""Tests for the /api/discord-users CRUD endpoints.

Covers the auth gate, create/update (upsert), lookup, listing, and delete —
the REST surface that lets a human link a GitHub login to a Discord user ID
for @-mention resolution in discord_notifier.mention_or_login.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from helpers import _sync_session, make_auth_token
from models import DiscordUser
from sqlalchemy import select


def _login(db_url: str, user_id: str = "gh-user-test", username: str = "testuser") -> str:
    return make_auth_token(db_url, user_id=user_id, username=username)


def test_list_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/api/discord-users")
    assert resp.status_code == 401


def test_upsert_creates_mapping(client):
    test_client, db_url = client
    token = _login(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    resp = test_client.put(
        "/api/discord-users/alice",
        json={"discord_user_id": "123456"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["github_login"] == "alice"
    assert body["discord_user_id"] == "123456"

    with _sync_session(db_url) as session:
        row = session.scalar(select(DiscordUser).where(DiscordUser.github_login == "alice"))
        assert row is not None
        assert row.discord_user_id == "123456"


def test_upsert_is_idempotent_and_updates_existing(client):
    test_client, db_url = client
    token = _login(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    test_client.put("/api/discord-users/bob", json={"discord_user_id": "111"}, headers=headers)
    resp = test_client.put(
        "/api/discord-users/bob", json={"discord_user_id": "222"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["discord_user_id"] == "222"

    resp = test_client.get("/api/discord-users/bob", headers=headers)
    assert resp.json()["discord_user_id"] == "222"


def test_upsert_lowercases_login(client):
    test_client, db_url = client
    token = _login(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    test_client.put(
        "/api/discord-users/CarolCase", json={"discord_user_id": "333"}, headers=headers
    )
    resp = test_client.get("/api/discord-users/carolcase", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["github_login"] == "carolcase"


def test_get_unknown_login_returns_404(client):
    test_client, db_url = client
    token = _login(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    resp = test_client.get("/api/discord-users/nonexistent-user", headers=headers)
    assert resp.status_code == 404


def test_delete_removes_mapping(client):
    test_client, db_url = client
    token = _login(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    test_client.put("/api/discord-users/dave", json={"discord_user_id": "444"}, headers=headers)
    resp = test_client.delete("/api/discord-users/dave", headers=headers)
    assert resp.status_code == 200

    resp = test_client.get("/api/discord-users/dave", headers=headers)
    assert resp.status_code == 404


def test_list_includes_created_mappings(client):
    test_client, db_url = client
    token = _login(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    test_client.put("/api/discord-users/erin", json={"discord_user_id": "555"}, headers=headers)
    resp = test_client.get("/api/discord-users", headers=headers)
    assert resp.status_code == 200
    logins = {row["github_login"] for row in resp.json()}
    assert "erin" in logins
