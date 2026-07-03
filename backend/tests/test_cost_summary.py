"""Tests for the GET /guilds/{guild_id}/cost-summary endpoint.

Requires the postgres-test container (localhost:5433); see AGENTS.md.
"""

from __future__ import annotations

from helpers import insert_guild, insert_task, insert_worker, make_auth_token


def _auth(db_url: str) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_url)}"}


# ---------------------------------------------------------------------------
# Basic shape tests
# ---------------------------------------------------------------------------


def test_cost_summary_empty_guild(client):
    test_client, db_url = client
    insert_guild(db_url, "gcst01")

    resp = test_client.get("/guilds/gcst01/cost-summary", headers=_auth(db_url))
    assert resp.status_code == 200
    assert resp.json() == []


def test_cost_summary_unknown_guild_404(client):
    test_client, db_url = client

    resp = test_client.get("/guilds/nope99/cost-summary", headers=_auth(db_url))
    assert resp.status_code == 404


def test_cost_summary_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "gcst02")

    resp = test_client.get("/guilds/gcst02/cost-summary")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Grouped result tests
# ---------------------------------------------------------------------------


def test_cost_summary_groups_by_tier_and_model(client):
    """Tasks are grouped by (model_tier, model) with correct counts."""
    test_client, db_url = client
    insert_guild(db_url, "gcst03")
    insert_worker(db_url, "gcst03", "w-cst03")

    # 2 standard sonnet done tasks
    insert_task(
        db_url,
        "gcst03",
        "t-cs3a",
        worker_id="w-cst03",
        state="done",
        model="claude-sonnet-4-6",
        model_tier="standard",
    )
    insert_task(
        db_url,
        "gcst03",
        "t-cs3b",
        worker_id="w-cst03",
        state="done",
        model="claude-sonnet-4-6",
        model_tier="standard",
    )
    # 1 cheap haiku done task
    insert_task(
        db_url,
        "gcst03",
        "t-cs3c",
        worker_id="w-cst03",
        state="done",
        model="claude-haiku-4-5-20251001",
        model_tier="cheap",
    )
    # 1 pending task — should NOT appear in the cost summary
    insert_task(
        db_url,
        "gcst03",
        "t-cs3d",
        worker_id="w-cst03",
        state="pending",
        model="claude-sonnet-4-6",
        model_tier="standard",
    )

    resp = test_client.get("/guilds/gcst03/cost-summary", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert isinstance(data, list)
    by_tier = {r["model_tier"]: r for r in data}

    assert "standard" in by_tier
    assert by_tier["standard"]["task_count"] == 2
    assert by_tier["standard"]["model"] == "claude-sonnet-4-6"

    assert "cheap" in by_tier
    assert by_tier["cheap"]["task_count"] == 1
    assert by_tier["cheap"]["model"] == "claude-haiku-4-5-20251001"

    # Pending task excluded — only 2 groups total
    assert len(data) == 2


def test_cost_summary_null_model_tier_included(client):
    """Tasks with NULL model_tier (legacy) are returned without breaking the query."""
    test_client, db_url = client
    insert_guild(db_url, "gcst04")
    insert_worker(db_url, "gcst04", "w-cst04")

    insert_task(
        db_url,
        "gcst04",
        "t-cs4a",
        worker_id="w-cst04",
        state="done",
        model=None,
        model_tier=None,
    )

    resp = test_client.get("/guilds/gcst04/cost-summary", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data) == 1
    row = data[0]
    assert row["model_tier"] is None
    assert row["model"] is None
    assert row["task_count"] == 1
    assert row["estimated_cost_usd"] is None


def test_cost_summary_failed_tasks_counted(client):
    """Tasks in 'failed' state are included alongside 'done' tasks."""
    test_client, db_url = client
    insert_guild(db_url, "gcst05")
    insert_worker(db_url, "gcst05", "w-cst05")

    insert_task(
        db_url,
        "gcst05",
        "t-cs5a",
        worker_id="w-cst05",
        state="done",
        model="claude-sonnet-4-6",
        model_tier="standard",
    )
    insert_task(
        db_url,
        "gcst05",
        "t-cs5b",
        worker_id="w-cst05",
        state="failed",
        model="claude-sonnet-4-6",
        model_tier="standard",
    )

    resp = test_client.get("/guilds/gcst05/cost-summary", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data) == 1
    assert data[0]["task_count"] == 2


def test_cost_summary_response_shape(client):
    """Response rows have exactly the expected keys."""
    test_client, db_url = client
    insert_guild(db_url, "gcst06")
    insert_worker(db_url, "gcst06", "w-cst06")

    insert_task(
        db_url,
        "gcst06",
        "t-cs6a",
        worker_id="w-cst06",
        state="done",
        model="claude-sonnet-4-6",
        model_tier="standard",
    )

    resp = test_client.get("/guilds/gcst06/cost-summary", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    row = data[0]
    assert set(row.keys()) == {"model_tier", "model", "task_count", "estimated_cost_usd"}


def test_cost_summary_days_param(client):
    """The ?days= param limits the look-back window."""
    test_client, db_url = client
    insert_guild(db_url, "gcst07")
    insert_worker(db_url, "gcst07", "w-cst07")

    # This task's created_at is now — should appear with days=1
    insert_task(
        db_url,
        "gcst07",
        "t-cs7a",
        worker_id="w-cst07",
        state="done",
        model="claude-sonnet-4-6",
        model_tier="standard",
    )

    resp = test_client.get("/guilds/gcst07/cost-summary?days=1", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    assert data[0]["task_count"] == 1
