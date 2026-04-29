"""Tests for worker and task REST endpoints."""

from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, make_auth_token


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

def test_list_workers_empty(client):
    test_client, db_path = client
    insert_guild(db_path, "guild01")
    resp = test_client.get("/guilds/guild01/workers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_worker_returns_id(client):
    test_client, db_path = client
    insert_guild(db_path, "guild02")
    resp = test_client.post(
        "/guilds/guild02/workers",
        json={"repos": ["owner/repo-a"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"].startswith("w-")
    assert data["repos"] == ["owner/repo-a"]
    assert "name" in data


def test_create_worker_appears_in_list(client):
    test_client, db_path = client
    insert_guild(db_path, "guild03")
    test_client.post("/guilds/guild03/workers", json={"repos": ["org/backend"]})
    resp = test_client.get("/guilds/guild03/workers")
    assert resp.status_code == 200
    workers = resp.json()
    assert len(workers) == 1
    assert workers[0]["guild_id"] == "guild03"


def test_create_multiple_workers(client):
    test_client, db_path = client
    insert_guild(db_path, "guild04")
    test_client.post("/guilds/guild04/workers", json={"repos": []})
    test_client.post("/guilds/guild04/workers", json={"repos": ["x/y"]})
    resp = test_client.get("/guilds/guild04/workers")
    assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def _create_worker(test_client, guild_id: str) -> str:
    resp = test_client.post(f"/guilds/{guild_id}/workers", json={"repos": []})
    return resp.json()["id"]


def test_list_tasks_empty(client):
    test_client, db_path = client
    insert_guild(db_path, "guild05")
    worker_id = _create_worker(test_client, "guild05")
    resp = test_client.get(f"/guilds/guild05/workers/{worker_id}/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_assign_task_unknown_worker(client):
    test_client, db_path = client
    insert_guild(db_path, "guild06")
    resp = test_client.post(
        "/guilds/guild06/workers/w-nosuch/tasks",
        json={"description": "do something", "tool": "claude"},
    )
    assert resp.status_code == 404


def test_assign_task_returns_pending(client):
    test_client, db_path = client
    insert_guild(db_path, "guild07")
    worker_id = _create_worker(test_client, "guild07")

    resp = test_client.post(
        f"/guilds/guild07/workers/{worker_id}/tasks",
        json={"description": "Write a hello-world script", "tool": "claude"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "pending"
    assert data["worker_id"] == worker_id
    assert data["id"].startswith("t-")


def test_assign_task_appears_in_list(client):
    test_client, db_path = client
    insert_guild(db_path, "guild08")
    worker_id = _create_worker(test_client, "guild08")

    test_client.post(
        f"/guilds/guild08/workers/{worker_id}/tasks",
        json={"description": "Task one", "tool": "claude"},
    )
    test_client.post(
        f"/guilds/guild08/workers/{worker_id}/tasks",
        json={"description": "Task two", "tool": "codex"},
    )

    resp = test_client.get(f"/guilds/guild08/workers/{worker_id}/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 2
    descriptions = {t["description"] for t in tasks}
    assert descriptions == {"Task one", "Task two"}


def test_guild_task_list(client):
    """GET /guilds/{id}/tasks lists tasks across all workers in the guild."""
    test_client, db_path = client
    insert_guild(db_path, "guild09")
    w1 = _create_worker(test_client, "guild09")
    w2 = _create_worker(test_client, "guild09")

    test_client.post(f"/guilds/guild09/workers/{w1}/tasks",
                     json={"description": "Alpha", "tool": "claude"})
    test_client.post(f"/guilds/guild09/workers/{w2}/tasks",
                     json={"description": "Beta", "tool": "claude"})

    resp = test_client.get("/guilds/guild09/tasks")
    assert resp.status_code == 200
    descriptions = {t["description"] for t in resp.json()}
    assert "Alpha" in descriptions
    assert "Beta" in descriptions
