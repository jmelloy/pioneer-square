"""Tests for the DEBUG_TOKEN-gated /debug/... endpoints (issue #879 follow-up).

``main.py`` only mounts ``routes.debug_query.router`` when the ``DEBUG_TOKEN``
env var is set — the routes don't exist at all otherwise. The real ``main``
module is imported once per test session (see conftest.py), so it can't be
reloaded per test to exercise that gating. Instead, these tests build a small
throwaway FastAPI app around the same router using the identical condition
main.py uses (``if os.environ.get("DEBUG_TOKEN"): app.include_router(...)``),
which faithfully exercises the gating logic without touching the shared app.
DB access still goes through the shared test database, since the `client`
fixture patches ``database.AsyncSessionLocal`` at the module level.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, insert_task  # noqa: E402

from routes import debug_query  # noqa: E402

_TOKEN = "test-debug-token-xyz"


def _debug_app_client(monkeypatch, *, token: str | None = _TOKEN) -> TestClient:
    """Build a fresh app mounting the debug router iff DEBUG_TOKEN is set,
    mirroring main.py's conditional app.include_router call exactly."""
    if token is None:
        monkeypatch.delenv("DEBUG_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DEBUG_TOKEN", token)
    app = FastAPI()
    if os.environ.get("DEBUG_TOKEN"):
        app.include_router(debug_query.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Conditional mounting
# ---------------------------------------------------------------------------


def test_debug_routes_absent_when_debug_token_unset(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch, token=None)
    resp = dc.get("/debug/tasks")
    assert resp.status_code == 404


def test_debug_routes_present_when_debug_token_set(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    # Route exists (not 404) even without auth — auth failure is 401, not 404.
    resp = dc.get("/debug/tasks")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth: missing / wrong / correct token
# ---------------------------------------------------------------------------


def test_debug_endpoint_missing_token_401(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    resp = dc.get("/debug/tasks")
    assert resp.status_code == 401


def test_debug_endpoint_wrong_token_403(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    resp = dc.get("/debug/tasks", headers={"Authorization": "Bearer not-the-token"})
    assert resp.status_code == 403


def test_debug_endpoint_correct_bearer_token_200(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg1")
    insert_task(db_url, "g-dbg1", "t-dbg1", state="working")
    resp = dc.get("/debug/tasks", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert "t-dbg1" in ids


def test_debug_endpoint_correct_x_debug_token_header_200(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg2")
    insert_task(db_url, "g-dbg2", "t-dbg2")
    resp = dc.get("/debug/tasks", headers={"X-Debug-Token": _TOKEN})
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert "t-dbg2" in ids


# ---------------------------------------------------------------------------
# GET /debug/tasks — full dump, cross-guild, includes soft-deleted
# ---------------------------------------------------------------------------


def test_debug_tasks_filters_by_guild(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg3-a")
    insert_guild(db_url, "g-dbg3-b")
    insert_task(db_url, "g-dbg3-a", "t-dbg3-a")
    insert_task(db_url, "g-dbg3-b", "t-dbg3-b")

    resp = dc.get(
        "/debug/tasks",
        params={"guild_id": "g-dbg3-a"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert ids == {"t-dbg3-a"}


def test_debug_tasks_logs_endpoint(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg4")
    insert_task(db_url, "g-dbg4", "t-dbg4")
    resp = dc.get(
        "/debug/tasks/t-dbg4/logs",
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /debug/query — raw SQL
# ---------------------------------------------------------------------------


def test_debug_raw_query_returns_matching_rows(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg5")
    insert_task(db_url, "g-dbg5", "t-dbg5a", state="working")
    insert_task(db_url, "g-dbg5", "t-dbg5b", state="done")

    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT * FROM tasks WHERE state = 'working'"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    ids = {r["id"] for r in rows}
    assert "t-dbg5a" in ids
    assert "t-dbg5b" not in ids


def test_debug_raw_query_requires_auth(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    resp = dc.post("/debug/query", json={"sql": "SELECT * FROM tasks"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO tasks (id) VALUES ('t-evil')",
        "UPDATE tasks SET state = 'done'",
        "DELETE FROM tasks",
        "DROP TABLE tasks",
        "SELECT * FROM tasks; DROP TABLE tasks;",
    ],
)
def test_debug_raw_query_rejects_write_operations(client, monkeypatch, sql):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    resp = dc.post(
        "/debug/query",
        json={"sql": sql},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 400


def test_debug_raw_query_rejects_disallowed_tables(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT * FROM github_tokens"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 400


def test_debug_raw_query_allows_operational_tables(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg6")
    insert_task(db_url, "g-dbg6", "t-dbg6")

    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT * FROM task_logs WHERE task_id = 't-dbg6'"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
