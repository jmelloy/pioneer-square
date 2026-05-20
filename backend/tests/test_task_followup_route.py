"""Tests for the user-initiated follow-up route (POST /guilds/{id}/tasks/{id}/followup).

Key distinction being tested: user-initiated follow-ups bypass the task_events
queue and dispatch directly.  Webhook-driven events that arrive while a task is
locked queue to task_events; the HTTP endpoint must NOT do that — it either
dispatches immediately or returns a 4xx so the user can retry.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import insert_guild, make_auth_token  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(db_path: str) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_path)}"}


def _create_worker(test_client, guild_id: str) -> str:
    return test_client.post(f"/guilds/{guild_id}/workers", json={"repos": []}).json()["id"]


def _create_task(test_client, guild_id: str, worker_id: str, db_path: str) -> str:
    resp = test_client.post(
        f"/guilds/{guild_id}/workers/{worker_id}/tasks",
        json={"description": "do the thing", "tool": "claude"},
        headers=_auth(db_path),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _set_task_branch(db_path: str, task_id: str, branch: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE tasks SET branch = ? WHERE id = ?", (branch, task_id))
        conn.commit()


def _set_task_locked(db_path: str, task_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET locked_at = ?, lock_holder = 'existing-holder' WHERE id = ?",
            (now, task_id),
        )
        conn.commit()


def _insert_idle_agent(db_path: str, guild_id: str, worker_id: str, agent_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM guilds WHERE guild_id = ? AND deleted_at IS NULL", (guild_id,)
        ).fetchone()
        guild_pk = row[0]
        conn.execute(
            "INSERT OR IGNORE INTO agents "
            "(id, guild_pk, worker_id, name, type, state, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (agent_id, guild_pk, worker_id, agent_id.upper(), "worker", "idle", now),
        )
        conn.commit()


def _task_event_count(db_path: str, task_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUserFollowupRoute:
    def test_dispatches_directly_no_task_event_queued(self, client):
        """Successful user follow-up dispatches immediately and creates no task_event row."""
        test_client, db_path = client
        insert_guild(db_path, "g-ufr-ok")
        worker_id = _create_worker(test_client, "g-ufr-ok")
        task_id = _create_task(test_client, "g-ufr-ok", worker_id, db_path)
        _set_task_branch(db_path, task_id, "claude/test-branch")
        _insert_idle_agent(db_path, "g-ufr-ok", worker_id, "a-ufr-ok")

        broadcast_calls = []

        async def capture(gid, msg):
            broadcast_calls.append(msg)

        with patch("routes.tasks.broadcast", side_effect=capture):
            resp = test_client.post(
                f"/guilds/g-ufr-ok/tasks/{task_id}/followup",
                json={"instructions": "Add more tests"},
                headers=_auth(db_path),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "dispatched"
        assert body["taskId"] == task_id
        assert body["workerId"] == worker_id

        # Exactly one task-followup broadcast with correct payload
        followup_msgs = [m for m in broadcast_calls if m.get("type") == "task-followup"]
        assert len(followup_msgs) == 1
        assert followup_msgs[0]["instructions"] == "Add more tests"
        assert followup_msgs[0]["branch"] == "claude/test-branch"
        assert followup_msgs[0]["workerId"] == worker_id

        # Crucially: no task_event rows — user-initiated follow-ups bypass the queue
        assert _task_event_count(db_path, task_id) == 0

    def test_returns_409_when_locked_and_does_not_queue(self, client):
        """When the task is locked, return 409 — do NOT silently enqueue in task_events."""
        test_client, db_path = client
        insert_guild(db_path, "g-ufr-locked")
        worker_id = _create_worker(test_client, "g-ufr-locked")
        task_id = _create_task(test_client, "g-ufr-locked", worker_id, db_path)
        _set_task_branch(db_path, task_id, "claude/locked-branch")
        _insert_idle_agent(db_path, "g-ufr-locked", worker_id, "a-ufr-locked")
        _set_task_locked(db_path, task_id)

        with patch("routes.tasks.broadcast", new_callable=AsyncMock):
            resp = test_client.post(
                f"/guilds/g-ufr-locked/tasks/{task_id}/followup",
                json={"instructions": "This should not queue"},
                headers=_auth(db_path),
            )

        assert resp.status_code == 409, resp.text
        assert (
            "locked" in resp.json()["detail"].lower()
            or "processing" in resp.json()["detail"].lower()
        )

        # No task_event row created — that's the key distinction vs webhook-driven events
        assert _task_event_count(db_path, task_id) == 0

    def test_returns_503_when_no_idle_worker(self, client):
        """Return 503 when no idle worker is available rather than silently queuing."""
        test_client, db_path = client
        insert_guild(db_path, "g-ufr-busy")
        worker_id = _create_worker(test_client, "g-ufr-busy")
        task_id = _create_task(test_client, "g-ufr-busy", worker_id, db_path)
        _set_task_branch(db_path, task_id, "claude/busy-branch")
        # No idle agent inserted — worker has no idle agent

        with patch("routes.tasks.broadcast", new_callable=AsyncMock):
            resp = test_client.post(
                f"/guilds/g-ufr-busy/tasks/{task_id}/followup",
                json={"instructions": "Fix lint"},
                headers=_auth(db_path),
            )

        assert resp.status_code == 503, resp.text
        assert "worker" in resp.json()["detail"].lower()

    def test_returns_400_when_task_has_no_branch(self, client):
        """Return 400 when the task has no branch recorded yet."""
        test_client, db_path = client
        insert_guild(db_path, "g-ufr-nobranch")
        worker_id = _create_worker(test_client, "g-ufr-nobranch")
        task_id = _create_task(test_client, "g-ufr-nobranch", worker_id, db_path)
        _insert_idle_agent(db_path, "g-ufr-nobranch", worker_id, "a-ufr-nb")
        # Branch is NULL by default from the task creation route

        with patch("routes.tasks.broadcast", new_callable=AsyncMock):
            resp = test_client.post(
                f"/guilds/g-ufr-nobranch/tasks/{task_id}/followup",
                json={"instructions": "Fix it"},
                headers=_auth(db_path),
            )

        assert resp.status_code == 400, resp.text

    def test_returns_404_for_unknown_task(self, client):
        test_client, db_path = client
        insert_guild(db_path, "g-ufr-missing")

        resp = test_client.post(
            "/guilds/g-ufr-missing/tasks/t-nosuch/followup",
            json={"instructions": "Fix it"},
            headers=_auth(db_path),
        )
        assert resp.status_code == 404

    def test_sets_lock_on_dispatch(self, client):
        """Dispatching a user follow-up must set locked_at and lock_holder on the task."""
        test_client, db_path = client
        insert_guild(db_path, "g-ufr-setlock")
        worker_id = _create_worker(test_client, "g-ufr-setlock")
        task_id = _create_task(test_client, "g-ufr-setlock", worker_id, db_path)
        _set_task_branch(db_path, task_id, "claude/lock-test-branch")
        _insert_idle_agent(db_path, "g-ufr-setlock", worker_id, "a-ufr-sl")

        with patch("routes.tasks.broadcast", new_callable=AsyncMock):
            resp = test_client.post(
                f"/guilds/g-ufr-setlock/tasks/{task_id}/followup",
                json={"instructions": "Lock this"},
                headers=_auth(db_path),
            )

        assert resp.status_code == 200
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT locked_at, lock_holder FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        assert row[0] is not None, "locked_at should be set after dispatch"
        assert row[1] is not None, "lock_holder should be set after dispatch"
