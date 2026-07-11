"""Tests for the worker-facing read-only DB query endpoints (issue #879).

GET /guilds/{guild_id}/db/tasks accepts structured filters; POST
/guilds/{guild_id}/db/query accepts a raw 'SELECT * FROM tasks ...' string.
Both require a worker auth_token or member login_token, matching the same
auth contract pinned in test_credentials_auth.py.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, insert_task, make_auth_token  # noqa: E402


def _register_worker(test_client, guild_id: str) -> dict:
    resp = test_client.post(f"/guilds/{guild_id}/workers", json={"repos": ["owner/repo"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# GET /guilds/{guild_id}/db/tasks — structured filters
# ---------------------------------------------------------------------------


def test_query_tasks_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq1")
    resp = test_client.get("/guilds/g-dbq1/db/tasks")
    assert resp.status_code == 401


def test_query_tasks_rejects_random_token(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq1b")
    resp = test_client.get(
        "/guilds/g-dbq1b/db/tasks",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


def test_query_tasks_accepts_worker_token_and_filters(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq2")
    worker = _register_worker(test_client, "g-dbq2")
    insert_task(
        db_url,
        "g-dbq2",
        "t-dbq2a",
        state="working",
        phase="execute",
        branch="feat/a",
        pr_url="https://github.com/o/r/pull/1",
        issue_number=42,
        issue_repo="o/r",
    )
    insert_task(db_url, "g-dbq2", "t-dbq2b", state="done", phase="execute")

    resp = test_client.get(
        "/guilds/g-dbq2/db/tasks",
        params={"state": "working"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "t-dbq2a"
    assert row["state"] == "working"
    assert row["branch"] == "feat/a"
    assert row["pr_url"] == "https://github.com/o/r/pull/1"
    assert row["issue_number"] == 42
    assert row["issue_repo"] == "o/r"
    assert set(row.keys()) == {
        "id",
        "name",
        "state",
        "phase",
        "branch",
        "pr_url",
        "issue_number",
        "issue_repo",
        "worker_id",
        "created_at",
        "deleted_at",
    }


def test_query_tasks_filters_by_issue_number_and_repo(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq3")
    worker = _register_worker(test_client, "g-dbq3")
    insert_task(db_url, "g-dbq3", "t-dbq3a", issue_number=7, issue_repo="acme/widgets")
    insert_task(db_url, "g-dbq3", "t-dbq3b", issue_number=8, issue_repo="acme/widgets")

    resp = test_client.get(
        "/guilds/g-dbq3/db/tasks",
        params={"issue_number": 7, "issue_repo": "acme/widgets"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert ids == {"t-dbq3a"}


def test_query_tasks_accepts_member_login_token(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq4")
    insert_task(db_url, "g-dbq4", "t-dbq4a")
    login_token = make_auth_token(db_url)
    resp = test_client.get(
        "/guilds/g-dbq4/db/tasks",
        headers={"Authorization": f"Bearer {login_token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_query_tasks_worker_token_scoped_to_its_own_guild(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq5-mine")
    insert_guild(db_url, "g-dbq5-other")
    insert_task(db_url, "g-dbq5-other", "t-dbq5-secret")
    worker = _register_worker(test_client, "g-dbq5-mine")
    resp = test_client.get(
        "/guilds/g-dbq5-other/db/tasks",
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /guilds/{guild_id}/db/query — raw SQL option
# ---------------------------------------------------------------------------


def test_raw_query_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq6")
    resp = test_client.post(
        "/guilds/g-dbq6/db/query",
        json={"sql": "SELECT * FROM tasks"},
    )
    assert resp.status_code == 401


def test_raw_query_returns_matching_rows(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq7")
    worker = _register_worker(test_client, "g-dbq7")
    insert_task(db_url, "g-dbq7", "t-dbq7a", state="working")
    insert_task(db_url, "g-dbq7", "t-dbq7b", state="done")

    resp = test_client.post(
        "/guilds/g-dbq7/db/query",
        json={"sql": "SELECT * FROM tasks WHERE state = 'working'"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == "t-dbq7a"
    assert rows[0]["state"] == "working"


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
def test_raw_query_rejects_write_operations(client, sql):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq8")
    worker = _register_worker(test_client, "g-dbq8")
    resp = test_client.post(
        "/guilds/g-dbq8/db/query",
        json={"sql": sql},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 400


def test_raw_query_rejects_other_tables(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq9")
    worker = _register_worker(test_client, "g-dbq9")
    resp = test_client.post(
        "/guilds/g-dbq9/db/query",
        json={"sql": "SELECT * FROM github_tokens"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 400


def test_raw_query_rejects_subquery_into_other_table(client):
    test_client, db_url = client
    insert_guild(db_url, "g-dbq10")
    worker = _register_worker(test_client, "g-dbq10")
    resp = test_client.post(
        "/guilds/g-dbq10/db/query",
        json={
            "sql": (
                "SELECT * FROM tasks WHERE worker_id IN "
                "(SELECT id FROM workers WHERE auth_token IS NOT NULL)"
            )
        },
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 400


def test_raw_query_scoped_to_guild_even_without_where_clause(client):
    """A caller's own guild-scoped auth_token must never surface another guild's rows,
    even when their raw SQL has no WHERE guild_id clause of its own."""
    test_client, db_url = client
    insert_guild(db_url, "g-dbq11-mine")
    insert_guild(db_url, "g-dbq11-other")
    insert_task(db_url, "g-dbq11-mine", "t-dbq11-mine")
    insert_task(db_url, "g-dbq11-other", "t-dbq11-secret")
    worker = _register_worker(test_client, "g-dbq11-mine")

    resp = test_client.post(
        "/guilds/g-dbq11-mine/db/query",
        json={"sql": "SELECT * FROM tasks"},
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert ids == {"t-dbq11-mine"}
