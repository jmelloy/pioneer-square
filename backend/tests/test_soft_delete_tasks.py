"""Tests for soft-delete on tasks (issue #177).

Covers:
- Migration adds the finalized_at column.
- _resolve_finalize_finalized_at honours finalized_at, expires_in_seconds, default.
- live_tasks_filter hides soft-deleted rows from the list endpoint.
- The finalize endpoint persists finalized_at to the row.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import _sync_session, insert_guild, make_auth_token, raw_conn  # noqa: E402
from models import Task
from sqlalchemy import select, update
from sqlmodel import col  # noqa: E402


def _auth(db_url: str) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_url)}"}


def _create_worker(test_client, guild_id: str) -> str:
    return test_client.post(f"/guilds/{guild_id}/workers", json={"repos": []}).json()["id"]


def _create_task(test_client, guild_id: str, worker_id: str, desc: str, db_url: str) -> str:
    resp = test_client.post(
        f"/guilds/{guild_id}/workers/{worker_id}/tasks",
        json={"description": desc, "tool": "claude"},
        headers=_auth(db_url),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _set_finalized_at(db_url: str, task_id: str, finalized_at: datetime | None) -> None:

    with _sync_session(db_url) as session:
        session.execute(update(Task).where(col(Task.id) == task_id).values(finalized_at=finalized_at))
        session.commit()


def _read_task(db_url: str, task_id: str) -> dict:

    with _sync_session(db_url) as session:
        row = session.execute(select(Task).where(col(Task.id) == task_id)).scalar_one_or_none()
    return dict(row.model_dump()) if row else {}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_adds_finalized_at_column(client):
    """The Alembic head must add tasks.finalized_at."""
    _, db_url = client
    with raw_conn(db_url) as (conn, cur):
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name = 'finalized_at'
        """)
        row = cur.fetchone()
    assert row is not None, "tasks.finalized_at column must exist"


# ---------------------------------------------------------------------------
# _resolve_finalize_finalized_at (REST helper)
# ---------------------------------------------------------------------------


def test_resolve_finalize_default_window(monkeypatch):
    from main import DEFAULT_FINALIZE_TTL, _resolve_finalize_finalized_at

    fixed = datetime(2026, 1, 1, tzinfo=UTC)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("routes.tasks.datetime", _FixedDT)
    out = _resolve_finalize_finalized_at(None)
    assert out == fixed + DEFAULT_FINALIZE_TTL


def test_resolve_finalize_explicit_seconds(monkeypatch):
    from main import FinalizeBody, _resolve_finalize_finalized_at

    fixed = datetime(2026, 1, 1, tzinfo=UTC)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr("routes.tasks.datetime", _FixedDT)
    out = _resolve_finalize_finalized_at(FinalizeBody(expires_in_seconds=1200))
    assert out == fixed + timedelta(seconds=1200)


def test_resolve_finalize_explicit_finalized_at_iso():
    from main import FinalizeBody, _resolve_finalize_finalized_at

    target = "2026-06-15T12:00:00+00:00"
    out = _resolve_finalize_finalized_at(FinalizeBody(finalized_at=target))
    assert out == datetime.fromisoformat(target)


def test_resolve_finalize_finalized_at_naive_assumed_utc():
    from main import FinalizeBody, _resolve_finalize_finalized_at

    out = _resolve_finalize_finalized_at(FinalizeBody(finalized_at="2026-06-15T12:00:00"))
    assert out.tzinfo is not None
    assert out == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def test_resolve_finalize_finalized_at_z_suffix():
    from main import FinalizeBody, _resolve_finalize_finalized_at

    out = _resolve_finalize_finalized_at(FinalizeBody(finalized_at="2026-06-15T12:00:00Z"))
    assert out == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def test_resolve_finalize_invalid_finalized_at_raises():
    from fastapi import HTTPException
    from main import FinalizeBody, _resolve_finalize_finalized_at

    with pytest.raises(HTTPException) as excinfo:
        _resolve_finalize_finalized_at(FinalizeBody(finalized_at="not-a-date"))
    assert excinfo.value.status_code == 400


def test_resolve_finalize_negative_seconds_raises():
    from fastapi import HTTPException
    from main import FinalizeBody, _resolve_finalize_finalized_at

    with pytest.raises(HTTPException) as excinfo:
        _resolve_finalize_finalized_at(FinalizeBody(expires_in_seconds=-1))
    assert excinfo.value.status_code == 400


def test_resolve_finalize_finalized_at_wins_over_seconds():
    """If both fields are set, finalized_at takes precedence."""
    from main import FinalizeBody, _resolve_finalize_finalized_at

    out = _resolve_finalize_finalized_at(
        FinalizeBody(finalized_at="2026-12-25T00:00:00Z", expires_in_seconds=60)
    )
    assert out == datetime(2026, 12, 25, tzinfo=UTC)


# ---------------------------------------------------------------------------
# live_tasks_filter — list endpoints hide soft-deleted rows
# ---------------------------------------------------------------------------


def test_list_guild_tasks_hides_past_finalized_at(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd01")
    worker_id = _create_worker(test_client, "gsd01")
    t_live = _create_task(test_client, "gsd01", worker_id, "live task", db_url)
    t_dead = _create_task(test_client, "gsd01", worker_id, "dead task", db_url)
    t_future = _create_task(test_client, "gsd01", worker_id, "future task", db_url)

    past = datetime.now(UTC) - timedelta(seconds=5)
    future = datetime.now(UTC) + timedelta(days=1)
    _set_finalized_at(db_url, t_dead, past)
    _set_finalized_at(db_url, t_future, future)

    resp = test_client.get("/guilds/gsd01/tasks", headers=_auth(db_url))
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert t_live in ids
    assert t_future in ids
    assert t_dead not in ids


def test_list_worker_tasks_hides_past_finalized_at(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd02")
    worker_id = _create_worker(test_client, "gsd02")
    t_live = _create_task(test_client, "gsd02", worker_id, "live", db_url)
    t_dead = _create_task(test_client, "gsd02", worker_id, "dead", db_url)
    _set_finalized_at(db_url, t_dead, datetime.now(UTC) - timedelta(minutes=1))

    resp = test_client.get(f"/guilds/gsd02/workers/{worker_id}/tasks")
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert ids == {t_live}


def test_get_task_logs_404_on_soft_deleted(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd03")
    worker_id = _create_worker(test_client, "gsd03")
    task_id = _create_task(test_client, "gsd03", worker_id, "task", db_url)
    _set_finalized_at(db_url, task_id, datetime.now(UTC) - timedelta(minutes=1))

    resp = test_client.get(f"/guilds/gsd03/tasks/{task_id}/logs", headers=_auth(db_url))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Finalize endpoint — persists finalized_at and supports overrides
# ---------------------------------------------------------------------------


def test_finalize_endpoint_default_sets_three_day_window(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd04")
    worker_id = _create_worker(test_client, "gsd04")
    task_id = _create_task(test_client, "gsd04", worker_id, "task", db_url)

    before = datetime.now(UTC)
    resp = test_client.post(f"/guilds/gsd04/tasks/{task_id}/finalize", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    finalized_at = resp.json()["finalizedAt"]
    parsed = datetime.fromisoformat(finalized_at)
    # Should be roughly 3 days from now (allow ±1 minute slack for slow machines).
    assert timedelta(days=3, minutes=-1) <= parsed - before <= timedelta(days=3, minutes=1)
    assert _read_task(db_url, task_id)["finalized_at"] == datetime.fromisoformat(finalized_at)


def test_finalize_endpoint_explicit_expires_in_seconds(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd05")
    worker_id = _create_worker(test_client, "gsd05")
    task_id = _create_task(test_client, "gsd05", worker_id, "task", db_url)

    before = datetime.now(UTC)
    resp = test_client.post(
        f"/guilds/gsd05/tasks/{task_id}/finalize",
        json={"expires_in_seconds": 1200},
        headers=_auth(db_url),
    )
    assert resp.status_code == 200, resp.text
    parsed = datetime.fromisoformat(resp.json()["finalizedAt"])
    assert timedelta(seconds=1199) <= parsed - before <= timedelta(seconds=1260)


def test_finalize_endpoint_explicit_finalized_at(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd06")
    worker_id = _create_worker(test_client, "gsd06")
    task_id = _create_task(test_client, "gsd06", worker_id, "task", db_url)

    target = "2026-12-25T00:00:00+00:00"
    resp = test_client.post(
        f"/guilds/gsd06/tasks/{task_id}/finalize",
        json={"finalized_at": target},
        headers=_auth(db_url),
    )
    assert resp.status_code == 200
    assert datetime.fromisoformat(resp.json()["finalizedAt"]) == datetime.fromisoformat(target)


def test_finalize_endpoint_rejects_bad_finalized_at(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd07")
    worker_id = _create_worker(test_client, "gsd07")
    task_id = _create_task(test_client, "gsd07", worker_id, "task", db_url)

    resp = test_client.post(
        f"/guilds/gsd07/tasks/{task_id}/finalize",
        json={"finalized_at": "garbage"},
        headers=_auth(db_url),
    )
    assert resp.status_code == 400


def test_finalize_then_list_hides_after_window_passes(client):
    """End-to-end: finalize with a tiny window, list excludes after it elapses."""
    test_client, db_url = client
    insert_guild(db_url, "gsd08")
    worker_id = _create_worker(test_client, "gsd08")
    task_id = _create_task(test_client, "gsd08", worker_id, "task", db_url)

    test_client.post(
        f"/guilds/gsd08/tasks/{task_id}/finalize",
        json={"expires_in_seconds": 0},
        headers=_auth(db_url),
    )
    # Backdate by 1 second so the strict `>` comparison hides it.
    past = datetime.now(UTC) - timedelta(seconds=1)
    _set_finalized_at(db_url, task_id, past)
    resp = test_client.get("/guilds/gsd08/tasks", headers=_auth(db_url))
    ids = {t["id"] for t in resp.json()}
    assert task_id not in ids


# ---------------------------------------------------------------------------
# Foreman tool helper — _resolve_finalize_finalized_at in foreman/tools.py
# ---------------------------------------------------------------------------


def test_foreman_tool_resolver_default():
    from foreman.tools import DEFAULT_FINALIZE_TTL_SECONDS, _resolve_finalize_finalized_at

    before = datetime.now(UTC)
    out, err = _resolve_finalize_finalized_at({})
    assert err is None
    assert out is not None
    delta = out - before
    assert (
        timedelta(seconds=DEFAULT_FINALIZE_TTL_SECONDS - 60)
        <= delta
        <= timedelta(seconds=DEFAULT_FINALIZE_TTL_SECONDS + 60)
    )


def test_foreman_tool_resolver_explicit_seconds():
    from foreman.tools import _resolve_finalize_finalized_at

    before = datetime.now(UTC)
    out, err = _resolve_finalize_finalized_at({"expires_in_seconds": 1200})
    assert err is None
    assert out is not None
    delta = out - before
    assert timedelta(seconds=1199) <= delta <= timedelta(seconds=1260)


def test_foreman_tool_resolver_invalid_seconds():
    from foreman.tools import _resolve_finalize_finalized_at

    out, err = _resolve_finalize_finalized_at({"expires_in_seconds": "not-an-int"})
    assert out is None
    assert err and "Invalid expires_in_seconds" in err


def test_foreman_tool_resolver_negative_seconds():
    from foreman.tools import _resolve_finalize_finalized_at

    out, err = _resolve_finalize_finalized_at({"expires_in_seconds": -5})
    assert out is None
    assert err and ">=" in err


def test_foreman_tool_resolver_explicit_finalized_at():
    from foreman.tools import _resolve_finalize_finalized_at

    target = "2026-12-25T00:00:00Z"
    out, err = _resolve_finalize_finalized_at({"finalized_at": target})
    assert err is None
    assert out == datetime(2026, 12, 25, tzinfo=UTC)


def test_foreman_tool_resolver_invalid_finalized_at():
    from foreman.tools import _resolve_finalize_finalized_at

    out, err = _resolve_finalize_finalized_at({"finalized_at": "garbage"})
    assert out is None
    assert err and "Invalid finalized_at" in err


def test_foreman_tool_definition_advertises_expiry_fields():
    """Schema must expose expires_in_seconds and finalized_at on finalize_task."""
    from foreman.tools import FOREMAN_TOOLS

    finalize = next(t for t in FOREMAN_TOOLS if t["name"] == "finalize_task")
    props = finalize["input_schema"]["properties"]
    assert "expires_in_seconds" in props
    assert props["expires_in_seconds"]["type"] == "integer"
    assert "finalized_at" in props
    assert props["finalized_at"]["type"] == "string"
    # task_id is the only required field; expiry fields stay optional.
    assert finalize["input_schema"]["required"] == ["task_id"]


def test_foreman_prompt_documents_expiry_windows():
    """The system prompt must mention each expiry window so the foreman picks them."""
    from foreman.prompt import FOREMAN_SYSTEM

    assert "1200" in FOREMAN_SYSTEM
    assert "259200" in FOREMAN_SYSTEM
    assert "86400" in FOREMAN_SYSTEM
    assert "expires_in_seconds" in FOREMAN_SYSTEM
