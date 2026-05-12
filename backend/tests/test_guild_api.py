"""Tests for guild REST endpoints."""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, make_auth_token


def test_get_guild_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/guilds/anything")
    assert resp.status_code == 401


def test_get_guild_not_found(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    resp = test_client.get(
        "/guilds/doesnotexist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


def test_get_guild_found(client):
    test_client, db_path = client
    insert_guild(db_path, "abc123", name="My Guild")
    token = make_auth_token(db_path)
    resp = test_client.get(
        "/guilds/abc123",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "abc123"
    assert data["name"] == "My Guild"
    assert "agents" in data
    assert "messages" in data


def test_get_guild_forbidden_for_non_member(client):
    test_client, db_path = client
    insert_guild(db_path, "private01", name="Private")
    other_token = make_auth_token(db_path, user_id="other-user", username="outsider")
    resp = test_client.get(
        "/guilds/private01",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


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
    assert len(data["id"]) >= 2


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

    get_resp = test_client.get(f"/guilds/{guild_id}", headers=headers)
    assert get_resp.json()["name"] == "New Name"


def test_update_guild_not_found(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    resp = test_client.patch(
        "/guilds/nosuchguild",
        json={"name": "X"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


def test_update_guild_primary_repo(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = test_client.post("/guilds", json={"name": "Repo Guild"}, headers=headers)
    guild_id = create_resp.json()["id"]

    patch_resp = test_client.patch(
        f"/guilds/{guild_id}",
        json={"primary_repo": "owner/myrepo"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["primary_repo"] == "owner/myrepo"

    get_resp = test_client.get(f"/guilds/{guild_id}", headers=headers)
    assert get_resp.json()["primary_repo"] == "owner/myrepo"


def test_update_guild_clear_primary_repo(client):
    test_client, db_path = client
    token = make_auth_token(db_path)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = test_client.post("/guilds", json={"name": "Clear Repo Guild"}, headers=headers)
    guild_id = create_resp.json()["id"]

    test_client.patch(f"/guilds/{guild_id}", json={"primary_repo": "owner/repo"}, headers=headers)

    # Clear by sending null
    patch_resp = test_client.patch(
        f"/guilds/{guild_id}",
        json={"primary_repo": None},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["primary_repo"] is None


def test_primary_repo_in_foreman_prompt():
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from foreman.prompt import build_system_prompt

    # Primary repo is included when set
    prompt = build_system_prompt("[]", "[]", primary_repo="jmelloy/pioneer-square")
    assert "jmelloy/pioneer-square" in prompt
    assert "primary repository" in prompt.lower()

    # Primary repo line is absent when not set
    prompt_no_repo = build_system_prompt("[]", "[]")
    assert "primary repository" not in prompt_no_repo.lower()

    # Primary repo line is absent when explicitly None
    prompt_none = build_system_prompt("[]", "[]", primary_repo=None)
    assert "primary repository" not in prompt_none.lower()

    # extra_context still works alongside primary_repo
    prompt_both = build_system_prompt("[]", "[]", extra_context="some context", primary_repo="o/r")
    assert "o/r" in prompt_both
    assert "some context" in prompt_both
