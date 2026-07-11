"""Tests for the worker-facing read-only DB query endpoint (issue #879).

GET /guilds/{guild_id}/db/tasks accepts structured filters and requires a
worker auth_token or member login_token — but, per the #879 follow-up, that
token no longer has to belong to this specific guild: any known worker or
member may query any guild's task state. Raw SQL and other deep-inspection
queries were moved to the DEBUG_TOKEN-gated /debug/... routes; see
test_debug_query_api.py.
"""

from __future__ import annotations

import os
import sys

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


def test_query_tasks_worker_token_not_scoped_to_its_own_guild(client):
    """Per the #879 follow-up, the guild-membership check was intentionally
    dropped for this endpoint: a worker token issued by one guild can be used
    to look up another guild's task state."""
    test_client, db_url = client
    insert_guild(db_url, "g-dbq5-mine")
    insert_guild(db_url, "g-dbq5-other")
    insert_task(db_url, "g-dbq5-other", "t-dbq5-other-task")
    worker = _register_worker(test_client, "g-dbq5-mine")
    resp = test_client.get(
        "/guilds/g-dbq5-other/db/tasks",
        headers={"Authorization": f"Bearer {worker['auth_token']}"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert ids == {"t-dbq5-other-task"}
