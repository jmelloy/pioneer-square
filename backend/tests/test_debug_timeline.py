"""Tests for GET /guilds/{guild_id}/tasks/{task_id}/debug."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))

from helpers import _sync_session, insert_guild, insert_task, insert_worker, make_auth_token
from models import ClaudeUsage, TaskLog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col

# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


def _insert_task_logs(db_url: str, task_id: str, worker_id: str = "w-dbg-worker") -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(TaskLog).values(
                [
                    {
                        "task_id": task_id,
                        "timestamp": now,
                        "line": "Starting task...",
                        "worker_id": worker_id,
                        "level": "worker",
                    },
                    {
                        "task_id": task_id,
                        "timestamp": now,
                        "line": '{"tool": "Bash", "input": "ls -la"}',
                        "worker_id": worker_id,
                        "level": "claude",
                        "data": '{"tool": "Bash", "input": "ls -la"}',
                    },
                    {
                        "task_id": task_id,
                        "timestamp": now,
                        "line": "Task complete",
                        "worker_id": worker_id,
                        "level": "worker",
                    },
                ]
            )
        )
        session.commit()


def _insert_usage(db_url: str, guild_id: str, task_id: str) -> None:
    from models import Guild
    from sqlalchemy import select as sa_select

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            sa_select(col(Guild.id)).where(
                col(Guild.slug) == guild_id, col(Guild.deleted_at).is_(None)
            )
        )
        assert guild_pk is not None
        session.execute(
            pg_insert(ClaudeUsage).values(
                guild_id=guild_pk,
                task_id=task_id,
                kind="result",
                input_tokens=1500,
                output_tokens=300,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=0.0042,
                num_turns=5,
                stop_reason="end_turn",
                model="claude-sonnet-4-6",
                created_at=now,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_debug_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/guilds/abc/tasks/t-abc/debug")
    assert resp.status_code == 401


def test_debug_unknown_guild_returns_403_or_404(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    resp = test_client.get(
        "/guilds/no-such-guild-debug/tasks/t-abc/debug",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


def test_debug_unknown_task_returns_404(client):
    test_client, db_url = client
    guild_id = "dbg-guild-notask"
    insert_guild(db_url, guild_id)
    token = make_auth_token(db_url)
    resp = test_client.get(
        f"/guilds/{guild_id}/tasks/t-no-such-task/debug",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert "Task not found" in resp.json().get("detail", "")


def test_debug_basic_structure(client):
    """Endpoint returns task + timeline with expected fields."""
    test_client, db_url = client
    guild_id = "dbg-guild-basic"
    task_id = "t-dbg-basic01"
    worker_id = "w-dbg-basic01"

    insert_guild(db_url, guild_id)
    insert_worker(db_url, guild_id, worker_id)
    insert_task(
        db_url,
        guild_id,
        task_id,
        worker_id=worker_id,
        description="Test debug task",
        state="done",
        branch="claude/debug-test",
    )
    _insert_task_logs(db_url, task_id, worker_id)
    token = make_auth_token(db_url)

    resp = test_client.get(
        f"/guilds/{guild_id}/tasks/{task_id}/debug",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Top-level shape
    assert "task" in data
    assert "timeline" in data

    # Task record
    assert data["task"]["id"] == task_id
    assert data["task"]["description"] == "Test debug task"
    assert data["task"]["state"] == "done"

    # Timeline has at least task_created + 3 logs
    assert len(data["timeline"]) >= 4

    # Every entry has required fields
    for entry in data["timeline"]:
        assert "timestamp" in entry
        assert "source" in entry
        assert "event_type" in entry
        assert "summary" in entry
        assert "detail" in entry

    # task_created is present
    types = [e["event_type"] for e in data["timeline"]]
    assert "task_created" in types

    # Timeline is sorted ascending
    timestamps = [e["timestamp"] for e in data["timeline"] if e.get("timestamp")]
    assert timestamps == sorted(timestamps)


def test_debug_includes_usage(client):
    """Claude usage rows appear in the timeline."""
    test_client, db_url = client
    guild_id = "dbg-guild-usage"
    task_id = "t-dbg-usage01"
    worker_id = "w-dbg-usage01"

    insert_guild(db_url, guild_id)
    insert_worker(db_url, guild_id, worker_id)
    insert_task(db_url, guild_id, task_id, worker_id=worker_id, state="done")
    _insert_usage(db_url, guild_id, task_id)
    token = make_auth_token(db_url)

    resp = test_client.get(
        f"/guilds/{guild_id}/tasks/{task_id}/debug",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    usage_entries = [e for e in data["timeline"] if e["event_type"].startswith("usage_")]
    assert len(usage_entries) == 1
    assert usage_entries[0]["source"] == "worker"
    assert usage_entries[0]["event_type"] == "usage_result"
    assert "5 turns" in usage_entries[0]["summary"]


def test_debug_task_in_wrong_guild_returns_404(client):
    """Task belonging to a different guild is not visible."""
    test_client, db_url = client
    guild_a = "dbg-guild-a01"
    guild_b = "dbg-guild-b01"
    task_id = "t-dbg-guild-a01"

    insert_guild(db_url, guild_a)
    insert_guild(db_url, guild_b)
    insert_task(db_url, guild_a, task_id)

    # Auth token is owner of both guilds (make_auth_token uses the same user)
    # but the task belongs to guild_a only
    token = make_auth_token(db_url)
    resp = test_client.get(
        f"/guilds/{guild_b}/tasks/{task_id}/debug",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_debug_non_member_forbidden(client):
    """A user who is not a guild member gets 403."""
    test_client, db_url = client
    guild_id = "dbg-guild-forbidden"
    task_id = "t-dbg-forbidden01"

    insert_guild(db_url, guild_id, owner_user_id="gh-owner-user")
    insert_task(db_url, guild_id, task_id)

    outsider_token = make_auth_token(db_url, user_id="gh-outsider", username="outsider")
    resp = test_client.get(
        f"/guilds/{guild_id}/tasks/{task_id}/debug",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 403


def test_debug_skips_github_when_no_pr_url(client):
    """No warnings and no GitHub events when task has no PR URL."""
    test_client, db_url = client
    guild_id = "dbg-guild-nopr"
    task_id = "t-dbg-nopr01"

    insert_guild(db_url, guild_id)
    insert_task(db_url, guild_id, task_id, pr_url=None)
    token = make_auth_token(db_url)

    resp = test_client.get(
        f"/guilds/{guild_id}/tasks/{task_id}/debug",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "warnings" not in data
    github_entries = [e for e in data["timeline"] if e["source"] == "github"]
    assert github_entries == []
