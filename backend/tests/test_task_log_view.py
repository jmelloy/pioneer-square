"""Tests for the shareable ``/api/task/{task_id}/log`` viewer endpoint.

Guild-less by design (a commit can link to /task/t-abc123/log without knowing
the guild), so the guild is resolved from the task and membership enforced
after the fact. Covers the happy path, 404 for unknown/soft-deleted tasks, and
the auth/membership gates.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import _sync_session, insert_guild, make_auth_token  # noqa: E402
from models import SOFT_DELETE_GRACE, Task, TaskLog  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlmodel import col  # noqa: E402


def _auth(db_url: str, **kw) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_url, **kw)}"}


def _create_task(test_client, guild_id: str, db_url: str) -> tuple[str, str]:
    worker_id = test_client.post(f"/guilds/{guild_id}/workers", json={"repos": []}).json()["id"]
    resp = test_client.post(
        f"/guilds/{guild_id}/workers/{worker_id}/tasks",
        json={"description": "do the thing", "tool": "claude"},
        headers=_auth(db_url),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"], worker_id


def _add_logs(db_url: str, task_id: str, worker_id: str) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        for i, line in enumerate(["first line", "second line"]):
            session.add(
                TaskLog(
                    task_id=task_id,
                    timestamp=now + timedelta(seconds=i),
                    line=line,
                    worker_id=worker_id,
                )
            )
        session.commit()


def test_returns_metadata_and_logs(client):
    test_client, db_url = client
    insert_guild(db_url, "gtl01")
    task_id, worker_id = _create_task(test_client, "gtl01", db_url)
    _add_logs(db_url, task_id, worker_id)

    resp = test_client.get(f"/api/task/{task_id}/log", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    task = body["task"]
    assert task["id"] == task_id
    assert task["guild_id"] == "gtl01"
    assert task["worker_id"] == worker_id
    assert task["state"] == "pending"
    assert task["created_at"] and task["phase"]

    lines = [log["line"] for log in body["logs"]]
    assert lines == ["first line", "second line"]
    assert all(log["timestamp"] for log in body["logs"])
    # updated_at tracks the newest log line, since tasks has no updated_at column.
    assert task["updated_at"] == body["logs"][-1]["timestamp"]


def test_updated_at_falls_back_to_created_at_without_logs(client):
    test_client, db_url = client
    insert_guild(db_url, "gtl02")
    task_id, _ = _create_task(test_client, "gtl02", db_url)

    body = test_client.get(f"/api/task/{task_id}/log", headers=_auth(db_url)).json()
    assert body["logs"] == []
    assert body["task"]["updated_at"] == body["task"]["created_at"]


def test_unknown_task_is_404(client):
    test_client, db_url = client
    insert_guild(db_url, "gtl03")
    resp = test_client.get("/api/task/t-nope/log", headers=_auth(db_url))
    assert resp.status_code == 404


def test_soft_deleted_task_is_404(client):
    test_client, db_url = client
    insert_guild(db_url, "gtl04")
    task_id, _ = _create_task(test_client, "gtl04", db_url)
    with _sync_session(db_url) as session:
        session.execute(
            update(Task)
            .where(col(Task.id) == task_id)
            .values(deleted_at=datetime.now(UTC) - SOFT_DELETE_GRACE - timedelta(minutes=1))
        )
        session.commit()

    resp = test_client.get(f"/api/task/{task_id}/log", headers=_auth(db_url))
    assert resp.status_code == 404


def test_requires_authentication(client):
    test_client, db_url = client
    insert_guild(db_url, "gtl05")
    task_id, _ = _create_task(test_client, "gtl05", db_url)

    assert test_client.get(f"/api/task/{task_id}/log").status_code == 401


def test_non_member_is_rejected(client):
    test_client, db_url = client
    insert_guild(db_url, "gtl06")
    task_id, _ = _create_task(test_client, "gtl06", db_url)

    outsider = _auth(db_url, user_id="gh-user-outsider", username="outsider")
    assert test_client.get(f"/api/task/{task_id}/log", headers=outsider).status_code == 403
