"""Tests for tasks.finished_at (replaced the deleted_at soft delete).

A task is active while ``finished_at`` is NULL. It is stamped at
finalize/cancel, except successful tasks tied to a still-open issue, which stay
NULL until the issue closes. Finished rows are kept, just hidden from lists.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import _sync_session, insert_guild, make_auth_token, raw_conn  # noqa: E402
from models import Task, should_keep_until_issue_closed  # noqa: E402
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


def _set_finished_at(db_url: str, task_id: str, finished_at: datetime | None) -> None:
    with _sync_session(db_url) as session:
        session.execute(update(Task).where(col(Task.id) == task_id).values(finished_at=finished_at))
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


def test_migration_replaces_deleted_at_with_finished_at(client):
    _, db_url = client
    with raw_conn(db_url) as (conn, cur):
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'tasks' AND column_name IN ('deleted_at', 'finished_at')
        """)
        cols = {r["column_name"] for r in cur.fetchall()}
    assert cols == {"finished_at"}


# ---------------------------------------------------------------------------
# should_keep_until_issue_closed
# ---------------------------------------------------------------------------


def test_done_with_open_issue_is_kept():
    assert should_keep_until_issue_closed("done", 42, "open")
    assert should_keep_until_issue_closed("done", 42, None)  # unknown ⇒ treat as open


def test_other_outcomes_finish_now():
    assert not should_keep_until_issue_closed("done", 42, "closed")
    assert not should_keep_until_issue_closed("done", None, None)
    assert not should_keep_until_issue_closed("failed", 42, "open")
    assert not should_keep_until_issue_closed("cancelled", 42, "open")
    # A phase='issue' root IS the issue proxy — finalizing it finishes it.
    assert not should_keep_until_issue_closed("done", 42, "open", phase="issue")


# ---------------------------------------------------------------------------
# Listing hides finished tasks; logs stay readable
# ---------------------------------------------------------------------------


def test_list_hides_finished_tasks(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd01")
    worker_id = _create_worker(test_client, "gsd01")
    t_live = _create_task(test_client, "gsd01", worker_id, "live task", db_url)
    t_done = _create_task(test_client, "gsd01", worker_id, "finished", db_url)
    _set_finished_at(db_url, t_done, datetime.now(UTC))

    resp = test_client.get("/guilds/gsd01/tasks", headers=_auth(db_url))
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert t_live in ids
    assert t_done not in ids
    assert _read_task(db_url, t_done)["id"] == t_done  # row is kept


def test_get_task_logs_accessible_after_finish(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd03")
    worker_id = _create_worker(test_client, "gsd03")
    task_id = _create_task(test_client, "gsd03", worker_id, "task", db_url)
    _set_finished_at(db_url, task_id, datetime.now(UTC))
    resp = test_client.get(f"/guilds/gsd03/tasks/{task_id}/logs", headers=_auth(db_url))
    assert resp.status_code == 200
    assert resp.json() == []
    insert_guild(db_url, "gsd03-other")
    assert (
        test_client.get(
            f"/guilds/gsd03-other/tasks/{task_id}/logs", headers=_auth(db_url)
        ).status_code
        == 404
    )
    assert test_client.get(f"/guilds/gsd03/tasks/{task_id}/logs").status_code == 401
    outsider = {
        "Authorization": "Bearer "
        + make_auth_token(db_url, user_id="gh-archived-logs-outsider", username="outsider")
    }
    assert (
        test_client.get(f"/guilds/gsd03/tasks/{task_id}/logs", headers=outsider).status_code == 403
    )


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
    parsed = datetime.fromisoformat(resp.json()["finishedAt"])
    assert timedelta(minutes=-1) <= parsed - before <= timedelta(minutes=1)
    assert _read_task(db_url, task_id)["finished_at"] == parsed


def test_finalize_endpoint_keeps_open_issue_task_live(client):
    test_client, db_url = client
    insert_guild(db_url, "gsd05")
    worker_id = _create_worker(test_client, "gsd05")
    task_id = _create_task(test_client, "gsd05", worker_id, "task", db_url)
    _set_issue(db_url, task_id, 7, "open")

    resp = test_client.post(f"/guilds/gsd05/tasks/{task_id}/finalize", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    assert resp.json()["finishedAt"] is None
    row = _read_task(db_url, task_id)
    assert row["state"] == "done"
    assert row["finished_at"] is None  # still live — issue is open

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


def test_mcp_list_tasks_hides_finished(client):
    import asyncio

    import routes.mcp as mcp

    test_client, db_url = client
    insert_guild(db_url, "gsd09")
    worker_id = _create_worker(test_client, "gsd09")
    t_live = _create_task(test_client, "gsd09", worker_id, "live", db_url)
    t_done = _create_task(test_client, "gsd09", worker_id, "done", db_url)
    _set_finished_at(db_url, t_done, datetime.now(UTC))

    text = str(asyncio.run(mcp._list_tasks({"guild_id": "gsd09"}, "p")))
    assert t_live in text
    assert t_done not in text
