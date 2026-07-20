"""Tests for the DEBUG_TOKEN-gated ``/debug/query`` endpoint (issue #879 follow-up).

``main.py`` only mounts ``routes.debug_query.router`` when the ``DEBUG_TOKEN``
env var is set — the route doesn't exist at all otherwise. The real ``main``
module is imported once per test session (see conftest.py), so it can't be
reloaded per test to exercise that gating. Instead, these tests build a small
throwaway FastAPI app around the same router using the identical condition
main.py uses (``if os.environ.get("DEBUG_TOKEN"): app.include_router(...)``),
which faithfully exercises the gating logic without touching the shared app.
DB access still goes through the shared test database, since the `client`
fixture patches ``database.AsyncSessionLocal`` at the module level.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

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
    resp = dc.post("/debug/query", json={"sql": "SELECT * FROM tasks"})
    assert resp.status_code == 404


def test_debug_routes_present_when_debug_token_set(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    # Route exists (not 404) even without auth — auth failure is 401, not 404.
    resp = dc.post("/debug/query", json={"sql": "SELECT * FROM tasks"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth: missing / wrong / correct token
# ---------------------------------------------------------------------------


def test_debug_endpoint_missing_token_401(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    resp = dc.post("/debug/query", json={"sql": "SELECT * FROM tasks"})
    assert resp.status_code == 401


def test_debug_endpoint_wrong_token_403(client, monkeypatch):
    _, _ = client
    dc = _debug_app_client(monkeypatch)
    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT * FROM tasks"},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert resp.status_code == 403


def test_debug_endpoint_correct_bearer_token_200(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg1")
    insert_task(db_url, "g-dbg1", "t-dbg1", state="working")
    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT * FROM tasks WHERE id = 't-dbg1'"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert "t-dbg1" in ids


def test_debug_endpoint_correct_x_debug_token_header_200(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg2")
    insert_task(db_url, "g-dbg2", "t-dbg2")
    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT * FROM tasks WHERE id = 't-dbg2'"},
        headers={"X-Debug-Token": _TOKEN},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert "t-dbg2" in ids


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


# ---------------------------------------------------------------------------
# Colons: casts and ISO timestamps must be allowed, not blanket-rejected
# ---------------------------------------------------------------------------


def test_debug_raw_query_allows_double_colon_cast(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg7")
    insert_task(db_url, "g-dbg7", "t-dbg7")

    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT id, state::text FROM tasks WHERE id = 't-dbg7'"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert "t-dbg7" in ids


def test_debug_raw_query_allows_cast_function(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg8")
    insert_task(db_url, "g-dbg8", "t-dbg8")

    resp = dc.post(
        "/debug/query",
        json={"sql": "SELECT CAST(state AS TEXT) FROM tasks WHERE id = 't-dbg8'"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200


def test_debug_raw_query_allows_iso_timestamp_literal(client, monkeypatch):
    _, db_url = client
    dc = _debug_app_client(monkeypatch)
    insert_guild(db_url, "g-dbg9")
    insert_task(db_url, "g-dbg9", "t-dbg9")

    resp = dc.post(
        "/debug/query",
        json={
            "sql": "SELECT id FROM tasks WHERE created_at > '2020-01-01T00:00:00' AND id = 't-dbg9'"
        },
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert "t-dbg9" in ids


# ---------------------------------------------------------------------------
# Statement timeout
#
# Triggering a real 30s Postgres statement-timeout cancellation from an
# integration test would be slow and flaky (the endpoint's own keyword/table
# allowlist rules out the obvious ways to force a slow query, e.g. PG_SLEEP is
# explicitly forbidden). Instead, exercise the error-mapping branch directly:
# a fake session whose second `exec()` raises the same asyncpg
# QueryCanceledError shape Postgres raises on a real timeout.
# ---------------------------------------------------------------------------


class _FakeQueryCanceledError(Exception):
    def __str__(self):
        return "canceling statement due to statement timeout"


class _FakeTimeoutSession:
    """Mimics AsyncSession.exec: the SET LOCAL call succeeds, the real query doesn't."""

    async def exec(self, stmt, params=None):
        if params is None:
            return None
        raise DBAPIError("SELECT ...", {}, _FakeQueryCanceledError())


def test_debug_raw_query_times_out_gracefully():
    req = debug_query.DebugRawQueryRequest(sql="SELECT id FROM tasks")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(debug_query.debug_raw_query(req, db=_FakeTimeoutSession()))
    assert exc_info.value.status_code == 504
    assert "timeout" in exc_info.value.detail.lower()


class _FakeOtherDbErrorSession:
    async def exec(self, stmt, params=None):
        if params is None:
            return None
        raise DBAPIError("SELECT ...", {}, RuntimeError("some other db failure"))


def test_debug_raw_query_other_db_errors_return_400_not_500():
    req = debug_query.DebugRawQueryRequest(sql="SELECT id FROM tasks")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(debug_query.debug_raw_query(req, db=_FakeOtherDbErrorSession()))
    assert exc_info.value.status_code == 400
