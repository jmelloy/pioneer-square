"""Tests for soft-delete on tasks.

Model: ``deleted_at`` records *when* a task was deleted (stamped now() at
finalize/cancel). A task is live while ``deleted_at`` is NULL or within
``SOFT_DELETE_GRACE`` of now. Successful tasks tied to a still-open issue are
left NULL until the issue closes.

Covers:
- Migration adds the deleted_at column.
- finalize_soft_delete_at: keep-alive-while-issue-open vs. stamp-now rules.
- live_tasks_filter hides rows deleted longer ago than the grace window.
- The finalize endpoint stamps now() (or NULL for an open-issue done task).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import _sync_session, insert_guild, make_auth_token, raw_conn  # noqa: E402
from models import SOFT_DELETE_GRACE, Task, finalize_soft_delete_at  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
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


def _set_deleted_at(db_url: str, task_id: str, deleted_at: datetime | None) -> None:
    with _sync_session(db_url) as session:
        session.execute(update(Task).where(col(Task.id) == task_id).values(deleted_at=deleted_at))
        session.commit()


def _set_issue(db_url: str, task_id: str, number: int, state: str) -> None:
    with _sync_session(db_url) as session:
        session.execute(
            update(Task)
            .where(col(Task.id) == task_id)
            .values(issue_repo="o/r", issue_number=number, issue_state=state)
        )
        session.commit()


def _read_task(db_url: str, task_id: str) -> dict:
    with _sync_session(db_url) as session:
        row = session.execute(select(Task).where(col(Task.id) == task_id)).scalar_one_or_none()
    return dict(row.model_dump()) if row else {}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_adds_deleted_at_column(client):
    """The Alembic head must add tasks.deleted_at."""
    _, db_url = client
    with raw_conn(db_url) as (conn, cur):
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name = 'deleted_at'
        """)
        row = cur.fetchone()
    assert row is not None, "tasks.deleted_at column must exist"


# ---------------------------------------------------------------------------
# finalize_soft_delete_at — the delete rule
# ---------------------------------------------------------------------------


def test_done_with_open_issue_stays_live():
    """A successful task on a still-open issue is not stamped."""
    assert finalize_soft_delete_at("done", 42, "open") is None
    assert finalize_soft_delete_at("done", 42, None) is None  # unknown ⇒ treat as open


def test_done_with_closed_issue_stamps_now():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert finalize_soft_delete_at("done", 42, "closed", now=now) == now


def test_done_without_issue_stamps_now():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert finalize_soft_delete_at("done", None, None, now=now) == now


def test_failed_and_cancelled_stamp_now_regardless_of_issue():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert finalize_soft_delete_at("failed", 42, "open", now=now) == now
    assert finalize_soft_delete_at("cancelled", 42, "open", now=now) == now


def test_issue_root_task_stamps_now_even_with_open_issue():
    """A phase='issue' root IS the issue proxy — finalizing it stamps immediately."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert finalize_soft_delete_at("done", 42, "open", phase="issue", now=now) == now


# ---------------------------------------------------------------------------
# live_tasks_filter — grace window
# ---------------------------------------------------------------------------


def test_list_hides_tasks_deleted_before_grace_window(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd01")
    worker_id = _create_worker(test_client, "gsd01")
    t_live = _create_task(test_client, "gsd01", worker_id, "live task", db_url)
    t_recent = _create_task(test_client, "gsd01", worker_id, "recently deleted", db_url)
    t_dead = _create_task(test_client, "gsd01", worker_id, "long deleted", db_url)

    now = datetime.now(UTC)
    _set_deleted_at(db_url, t_recent, now - (SOFT_DELETE_GRACE / 2))  # within grace ⇒ visible
    _set_deleted_at(db_url, t_dead, now - SOFT_DELETE_GRACE - timedelta(minutes=1))  # past ⇒ hidden

    resp = test_client.get("/guilds/gsd01/tasks", headers=_auth(db_url))
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert t_live in ids
    assert t_recent in ids
    assert t_dead not in ids


def test_get_task_logs_404_after_grace_window(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd03")
    worker_id = _create_worker(test_client, "gsd03")
    task_id = _create_task(test_client, "gsd03", worker_id, "task", db_url)
    _set_deleted_at(db_url, task_id, datetime.now(UTC) - SOFT_DELETE_GRACE - timedelta(minutes=1))

    resp = test_client.get(f"/guilds/gsd03/tasks/{task_id}/logs", headers=_auth(db_url))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Finalize endpoint
# ---------------------------------------------------------------------------


def test_finalize_endpoint_stamps_now(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd04")
    worker_id = _create_worker(test_client, "gsd04")
    task_id = _create_task(test_client, "gsd04", worker_id, "task", db_url)

    before = datetime.now(UTC)
    resp = test_client.post(f"/guilds/gsd04/tasks/{task_id}/finalize", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    parsed = datetime.fromisoformat(resp.json()["deletedAt"])
    assert timedelta(minutes=-1) <= parsed - before <= timedelta(minutes=1)
    assert _read_task(db_url, task_id)["deleted_at"] == parsed


def test_finalize_endpoint_keeps_open_issue_task_live(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd05")
    worker_id = _create_worker(test_client, "gsd05")
    task_id = _create_task(test_client, "gsd05", worker_id, "task", db_url)
    _set_issue(db_url, task_id, 7, "open")

    resp = test_client.post(f"/guilds/gsd05/tasks/{task_id}/finalize", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    assert resp.json()["deletedAt"] is None
    row = _read_task(db_url, task_id)
    assert row["state"] == "done"
    assert row["deleted_at"] is None  # still live — issue is open

    # Still listed while the issue stays open.
    ids = {t["id"] for t in test_client.get("/guilds/gsd05/tasks", headers=_auth(db_url)).json()}
    assert task_id in ids


# ---------------------------------------------------------------------------
# Foreman tool schema + prompt no longer expose an expiry knob
# ---------------------------------------------------------------------------


def test_finalize_task_schema_has_no_expiry_fields():
    from foreman.tools import FOREMAN_TOOLS

    finalize = next(t for t in FOREMAN_TOOLS if t["name"] == "finalize_task")
    props = finalize["input_schema"]["properties"]
    assert "expires_in_seconds" not in props
    assert "deleted_at" not in props
    assert set(props) == {"task_id", "outcome"}
    assert finalize["input_schema"]["required"] == ["task_id"]


def test_foreman_prompt_drops_expiry_knob():
    from foreman.prompt import FOREMAN_SYSTEM

    assert "expires_in_seconds" not in FOREMAN_SYSTEM
