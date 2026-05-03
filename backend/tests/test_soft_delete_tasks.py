"""Tests for soft-delete on tasks (issue #177).

Covers:
- Migration adds the deleted_at column.
- _resolve_finalize_deleted_at honours deleted_at, expires_in_seconds, default.
- live_tasks_filter hides soft-deleted rows from the list endpoint.
- The finalize endpoint persists deleted_at to the row.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import insert_guild, make_auth_token  # noqa: E402


def _auth(db_path: str) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_path)}"}


def _create_worker(test_client, guild_id: str) -> str:
    return test_client.post(f"/guilds/{guild_id}/workers", json={"repos": []}).json()["id"]


def _create_task(test_client, guild_id: str, worker_id: str, desc: str, db_path: str) -> str:
    resp = test_client.post(
        f"/guilds/{guild_id}/workers/{worker_id}/tasks",
        json={"description": desc, "tool": "claude"},
        headers=_auth(db_path),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _set_deleted_at(db_path: str, task_id: str, deleted_at: str | None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE tasks SET deleted_at = ? WHERE id = ?", (deleted_at, task_id))
        conn.commit()


def _read_task(db_path: str, task_id: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_adds_deleted_at_column(client):
    """The Alembic head must add tasks.deleted_at."""
    _, db_path = client
    with sqlite3.connect(db_path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    assert "deleted_at" in cols


# ---------------------------------------------------------------------------
# _resolve_finalize_deleted_at (REST helper)
# ---------------------------------------------------------------------------


def test_resolve_finalize_default_window(monkeypatch):
    from main import DEFAULT_FINALIZE_TTL, _resolve_finalize_deleted_at

    fixed = datetime(2026, 1, 1, tzinfo=UTC)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("main.datetime", _FixedDT)
    out = _resolve_finalize_deleted_at(None)
    assert datetime.fromisoformat(out) == fixed + DEFAULT_FINALIZE_TTL


def test_resolve_finalize_explicit_seconds(monkeypatch):
    from main import FinalizeBody, _resolve_finalize_deleted_at

    fixed = datetime(2026, 1, 1, tzinfo=UTC)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("main.datetime", _FixedDT)
    out = _resolve_finalize_deleted_at(FinalizeBody(expires_in_seconds=1200))
    assert datetime.fromisoformat(out) == fixed + timedelta(seconds=1200)


def test_resolve_finalize_explicit_deleted_at_iso():
    from main import FinalizeBody, _resolve_finalize_deleted_at

    target = "2026-06-15T12:00:00+00:00"
    out = _resolve_finalize_deleted_at(FinalizeBody(deleted_at=target))
    assert datetime.fromisoformat(out) == datetime.fromisoformat(target)


def test_resolve_finalize_deleted_at_naive_assumed_utc():
    from main import FinalizeBody, _resolve_finalize_deleted_at

    out = _resolve_finalize_deleted_at(FinalizeBody(deleted_at="2026-06-15T12:00:00"))
    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def test_resolve_finalize_deleted_at_z_suffix():
    from main import FinalizeBody, _resolve_finalize_deleted_at

    out = _resolve_finalize_deleted_at(FinalizeBody(deleted_at="2026-06-15T12:00:00Z"))
    assert datetime.fromisoformat(out) == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def test_resolve_finalize_invalid_deleted_at_raises():
    from fastapi import HTTPException

    from main import FinalizeBody, _resolve_finalize_deleted_at

    with pytest.raises(HTTPException) as excinfo:
        _resolve_finalize_deleted_at(FinalizeBody(deleted_at="not-a-date"))
    assert excinfo.value.status_code == 400


def test_resolve_finalize_negative_seconds_raises():
    from fastapi import HTTPException

    from main import FinalizeBody, _resolve_finalize_deleted_at

    with pytest.raises(HTTPException) as excinfo:
        _resolve_finalize_deleted_at(FinalizeBody(expires_in_seconds=-1))
    assert excinfo.value.status_code == 400


def test_resolve_finalize_deleted_at_wins_over_seconds():
    """If both fields are set, deleted_at takes precedence."""
    from main import FinalizeBody, _resolve_finalize_deleted_at

    out = _resolve_finalize_deleted_at(
        FinalizeBody(deleted_at="2026-12-25T00:00:00Z", expires_in_seconds=60)
    )
    assert datetime.fromisoformat(out) == datetime(2026, 12, 25, tzinfo=UTC)


# ---------------------------------------------------------------------------
# live_tasks_filter — list endpoints hide soft-deleted rows
# ---------------------------------------------------------------------------


def test_list_guild_tasks_hides_past_deleted_at(client):
    test_client, db_path = client
    insert_guild(db_path, "gsd01")
    worker_id = _create_worker(test_client, "gsd01")
    t_live = _create_task(test_client, "gsd01", worker_id, "live task", db_path)
    t_dead = _create_task(test_client, "gsd01", worker_id, "dead task", db_path)
    t_future = _create_task(test_client, "gsd01", worker_id, "future task", db_path)

    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    _set_deleted_at(db_path, t_dead, past)
    _set_deleted_at(db_path, t_future, future)

    resp = test_client.get("/guilds/gsd01/tasks", headers=_auth(db_path))
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert t_live in ids
    assert t_future in ids
    assert t_dead not in ids


def test_list_worker_tasks_hides_past_deleted_at(client):
    test_client, db_path = client
    insert_guild(db_path, "gsd02")
    worker_id = _create_worker(test_client, "gsd02")
    t_live = _create_task(test_client, "gsd02", worker_id, "live", db_path)
    t_dead = _create_task(test_client, "gsd02", worker_id, "dead", db_path)
    _set_deleted_at(db_path, t_dead, (datetime.now(UTC) - timedelta(minutes=1)).isoformat())

    resp = test_client.get(f"/guilds/gsd02/workers/{worker_id}/tasks")
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert ids == {t_live}


def test_get_task_logs_404_on_soft_deleted(client):
    test_client, db_path = client
    insert_guild(db_path, "gsd03")
    worker_id = _create_worker(test_client, "gsd03")
    task_id = _create_task(test_client, "gsd03", worker_id, "task", db_path)
    _set_deleted_at(db_path, task_id, (datetime.now(UTC) - timedelta(minutes=1)).isoformat())

    resp = test_client.get(f"/guilds/gsd03/tasks/{task_id}/logs", headers=_auth(db_path))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Finalize endpoint — persists deleted_at and supports overrides
# ---------------------------------------------------------------------------


def test_finalize_endpoint_default_sets_three_day_window(client):
    test_client, db_path = client
    insert_guild(db_path, "gsd04")
    worker_id = _create_worker(test_client, "gsd04")
    task_id = _create_task(test_client, "gsd04", worker_id, "task", db_path)

    before = datetime.now(UTC)
    resp = test_client.post(
        f"/guilds/gsd04/tasks/{task_id}/finalize", headers=_auth(db_path)
    )
    assert resp.status_code == 200, resp.text
    deleted_at = resp.json()["deletedAt"]
    parsed = datetime.fromisoformat(deleted_at)
    # Should be roughly 3 days from now (allow ±1 minute slack for slow machines).
    assert timedelta(days=3, minutes=-1) <= parsed - before <= timedelta(days=3, minutes=1)
    assert _read_task(db_path, task_id)["deleted_at"] == deleted_at


def test_finalize_endpoint_explicit_expires_in_seconds(client):
    test_client, db_path = client
    insert_guild(db_path, "gsd05")
    worker_id = _create_worker(test_client, "gsd05")
    task_id = _create_task(test_client, "gsd05", worker_id, "task", db_path)

    before = datetime.now(UTC)
    resp = test_client.post(
        f"/guilds/gsd05/tasks/{task_id}/finalize",
        json={"expires_in_seconds": 1200},
        headers=_auth(db_path),
    )
    assert resp.status_code == 200, resp.text
    parsed = datetime.fromisoformat(resp.json()["deletedAt"])
    assert timedelta(seconds=1199) <= parsed - before <= timedelta(seconds=1260)


def test_finalize_endpoint_explicit_deleted_at(client):
    test_client, db_path = client
    insert_guild(db_path, "gsd06")
    worker_id = _create_worker(test_client, "gsd06")
    task_id = _create_task(test_client, "gsd06", worker_id, "task", db_path)

    target = "2026-12-25T00:00:00+00:00"
    resp = test_client.post(
        f"/guilds/gsd06/tasks/{task_id}/finalize",
        json={"deleted_at": target},
        headers=_auth(db_path),
    )
    assert resp.status_code == 200
    assert datetime.fromisoformat(resp.json()["deletedAt"]) == datetime.fromisoformat(target)


def test_finalize_endpoint_rejects_bad_deleted_at(client):
    test_client, db_path = client
    insert_guild(db_path, "gsd07")
    worker_id = _create_worker(test_client, "gsd07")
    task_id = _create_task(test_client, "gsd07", worker_id, "task", db_path)

    resp = test_client.post(
        f"/guilds/gsd07/tasks/{task_id}/finalize",
        json={"deleted_at": "garbage"},
        headers=_auth(db_path),
    )
    assert resp.status_code == 400


def test_finalize_then_list_hides_after_window_passes(client):
    """End-to-end: finalize with a tiny window, list excludes after it elapses."""
    test_client, db_path = client
    insert_guild(db_path, "gsd08")
    worker_id = _create_worker(test_client, "gsd08")
    task_id = _create_task(test_client, "gsd08", worker_id, "task", db_path)

    test_client.post(
        f"/guilds/gsd08/tasks/{task_id}/finalize",
        json={"expires_in_seconds": 0},
        headers=_auth(db_path),
    )
    # Backdate by 1 second so the strict `>` comparison hides it.
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    _set_deleted_at(db_path, task_id, past)
    resp = test_client.get("/guilds/gsd08/tasks", headers=_auth(db_path))
    ids = {t["id"] for t in resp.json()}
    assert task_id not in ids


# ---------------------------------------------------------------------------
# Foreman tool helper — _resolve_finalize_deleted_at in foreman/tools.py
# ---------------------------------------------------------------------------


def test_foreman_tool_resolver_default():
    from foreman.tools import DEFAULT_FINALIZE_TTL_SECONDS, _resolve_finalize_deleted_at

    before = datetime.now(UTC)
    out, err = _resolve_finalize_deleted_at({})
    assert err is None
    delta = datetime.fromisoformat(out) - before
    assert (
        timedelta(seconds=DEFAULT_FINALIZE_TTL_SECONDS - 60)
        <= delta
        <= timedelta(seconds=DEFAULT_FINALIZE_TTL_SECONDS + 60)
    )


def test_foreman_tool_resolver_explicit_seconds():
    from foreman.tools import _resolve_finalize_deleted_at

    before = datetime.now(UTC)
    out, err = _resolve_finalize_deleted_at({"expires_in_seconds": 1200})
    assert err is None
    delta = datetime.fromisoformat(out) - before
    assert timedelta(seconds=1199) <= delta <= timedelta(seconds=1260)


def test_foreman_tool_resolver_invalid_seconds():
    from foreman.tools import _resolve_finalize_deleted_at

    out, err = _resolve_finalize_deleted_at({"expires_in_seconds": "not-an-int"})
    assert out == ""
    assert err and "Invalid expires_in_seconds" in err


def test_foreman_tool_resolver_negative_seconds():
    from foreman.tools import _resolve_finalize_deleted_at

    out, err = _resolve_finalize_deleted_at({"expires_in_seconds": -5})
    assert out == ""
    assert err and ">=" in err


def test_foreman_tool_resolver_explicit_deleted_at():
    from foreman.tools import _resolve_finalize_deleted_at

    target = "2026-12-25T00:00:00Z"
    out, err = _resolve_finalize_deleted_at({"deleted_at": target})
    assert err is None
    assert datetime.fromisoformat(out) == datetime(2026, 12, 25, tzinfo=UTC)


def test_foreman_tool_resolver_invalid_deleted_at():
    from foreman.tools import _resolve_finalize_deleted_at

    out, err = _resolve_finalize_deleted_at({"deleted_at": "garbage"})
    assert out == ""
    assert err and "Invalid deleted_at" in err


def test_foreman_tool_definition_advertises_expiry_fields():
    """Schema must expose expires_in_seconds and deleted_at on finalize_task."""
    from foreman.tools import FOREMAN_TOOLS

    finalize = next(t for t in FOREMAN_TOOLS if t["name"] == "finalize_task")
    props = finalize["input_schema"]["properties"]
    assert "expires_in_seconds" in props
    assert props["expires_in_seconds"]["type"] == "integer"
    assert "deleted_at" in props
    assert props["deleted_at"]["type"] == "string"
    # task_id is the only required field; expiry fields stay optional.
    assert finalize["input_schema"]["required"] == ["task_id"]


def test_foreman_prompt_documents_expiry_windows():
    """The system prompt must mention each expiry window so the foreman picks them."""
    from foreman.prompt import FOREMAN_SYSTEM

    assert "1200" in FOREMAN_SYSTEM
    assert "259200" in FOREMAN_SYSTEM
    assert "86400" in FOREMAN_SYSTEM
    assert "expires_in_seconds" in FOREMAN_SYSTEM
