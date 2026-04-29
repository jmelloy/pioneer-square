"""Tests for guild REST endpoints."""

from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, make_auth_token


def test_get_guild_not_found(client):
    test_client, _ = client
    resp = test_client.get("/guilds/doesnotexist")
    assert resp.status_code == 404


def test_get_guild_found(client):
    test_client, db_path = client
    insert_guild(db_path, "abc123", name="My Guild")
    resp = test_client.get("/guilds/abc123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "abc123"
    assert data["name"] == "My Guild"
    assert "agents" in data
    assert "messages" in data


def test_create_guild_requires_auth(client):
    test_client, _ = client
    resp = test_client.post("/guilds", json={})
    assert resp.status_code == 401


def test_create_guild_returns_id(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    resp = test_client.post(
        "/guilds",
        json={"name": "Test Guild"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["name"] == "Test Guild"
    assert len(data["id"]) == 6


def test_create_guild_default_name(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    resp = test_client.post(
        "/guilds",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"].startswith("Guild ")


def test_list_guilds_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/guilds")
    assert resp.status_code == 401


def test_list_guilds_returns_created_guild(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    headers = {"Authorization": f"Bearer {token}"}

    test_client.post("/guilds", json={"name": "Guild A"}, headers=headers)
    test_client.post("/guilds", json={"name": "Guild B"}, headers=headers)

    resp = test_client.get("/guilds", headers=headers)
    assert resp.status_code == 200
    names = [g["name"] for g in resp.json()]
    assert "Guild A" in names
    assert "Guild B" in names


def test_update_guild_name(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = test_client.post("/guilds", json={"name": "Old Name"}, headers=headers)
    guild_id = create_resp.json()["id"]

    patch_resp = test_client.patch(
        f"/guilds/{guild_id}",
        json={"name": "New Name"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "New Name"

    get_resp = test_client.get(f"/guilds/{guild_id}")
    assert get_resp.json()["name"] == "New Name"


def test_update_guild_not_found(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    resp = test_client.patch(
        "/guilds/nosuchguild",
        json={"name": "X"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
