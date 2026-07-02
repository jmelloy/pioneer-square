"""Tests for the GET /guilds/{guild_id}/tasks/tree endpoint.

Covers:
- Nodes return the real issue state from the DB cache when no GitHub token.
- Nodes default to "open" when DB has no cached state.
- Nodes return the cached issue title from the DB when no GitHub token.
- Successful GitHub API calls write back issue_state and issue_title to the DB.
- GitHub `issues` webhook events keep issue_state and issue_title current on linked tasks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import (  # noqa: E402
    _sync_session,
    insert_guild,
    insert_member,
    insert_task,
    insert_worker,
    make_auth_token,
    raw_conn,
)
from models import Guild, Task
from sqlalchemy import select, update
from sqlmodel import col

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(db_url: str, user_id: str = "gh-user-test") -> dict:
    token = make_auth_token(db_url, user_id=user_id, username=user_id)
    return {"Authorization": f"Bearer {token}"}


def _guild_no_owner_token(db_url: str, guild_id: str) -> None:
    """Insert a guild whose owner has no GitHub token.

    The requesting user (gh-user-test) is added as a member, so the endpoint
    is accessible but `gh_token` will be None because the owner has no token.
    """
    # owner-notokenuser gets created in users + guild_members(owner) but no github_tokens row
    insert_guild(db_url, guild_id, owner_user_id="owner-notokenuser")
    # Add the default test user as a member so they can call the endpoint
    insert_member(db_url, guild_id, "gh-user-test", role="member")


def _get_issue_state(db_url: str, task_id: str) -> str | None:
    """Read issue_state for a task directly from the DB."""
    with _sync_session(db_url) as session:
        row = session.execute(
            select(col(Task.issue_state)).where(col(Task.id) == task_id)
        ).one_or_none()
    return row[0] if row else None


def _get_issue_title(db_url: str, task_id: str) -> str | None:
    """Read issue_title for a task directly from the DB."""
    with _sync_session(db_url) as session:
        row = session.execute(
            select(col(Task.issue_title)).where(col(Task.id) == task_id)
        ).one_or_none()
    return row[0] if row else None


def _signed_headers(secret: str, body: bytes, *, event: str, delivery: str) -> dict:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={sig}",
        "Content-Type": "application/json",
    }


def _set_webhook_secret(db_url: str, guild_id: str, secret: str) -> None:
    with _sync_session(db_url) as session:
        session.execute(
            update(Guild).where(col(Guild.slug) == guild_id).values(webhook_secret=secret)
        )
        session.commit()


# ---------------------------------------------------------------------------
# Tree endpoint — DB fallback (no GitHub token)
# ---------------------------------------------------------------------------


def test_tree_returns_closed_state_from_db_when_no_token(client):
    """A task with issue_state='closed' in the DB returns state='closed' in the tree."""
    test_client, db_url = client
    guild_id = "tr-ntoken1"
    _guild_no_owner_token(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-tr1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-tr1",
        worker_id="w-tr1",
        state="done",
        issue_number=42,
        issue_repo="org/repo",
        issue_state="closed",
    )

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    nodes = data["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["issue_number"] == 42
    assert node["state"] == "closed", f"expected 'closed', got {node['state']!r}"


def test_tree_defaults_to_open_when_no_token_and_no_db_state(client):
    """A task with no cached issue_state returns state='open' (safe default)."""
    test_client, db_url = client
    guild_id = "tr-ntoken2"
    _guild_no_owner_token(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-tr2", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-tr2",
        worker_id="w-tr2",
        state="pending",
        issue_number=99,
        issue_repo="org/repo",
        issue_state=None,  # unknown — no prior fetch
    )

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    nodes = data["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["state"] == "open"


def test_tree_open_and_closed_issues_sorted_correctly(client):
    """Open issues appear before closed issues in the response."""
    test_client, db_url = client
    guild_id = "tr-sort1"
    _guild_no_owner_token(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-sort1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-sort-open",
        worker_id="w-sort1",
        state="pending",
        issue_number=10,
        issue_repo="org/repo",
        issue_state="open",
    )
    insert_task(
        db_url,
        guild_id,
        "task-sort-closed",
        worker_id="w-sort1",
        state="done",
        issue_number=5,
        issue_repo="org/repo",
        issue_state="closed",
    )

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert len(nodes) == 2
    # Open issues should come first
    assert nodes[0]["state"] == "open"
    assert nodes[1]["state"] == "closed"


# ---------------------------------------------------------------------------
# Tree endpoint — GitHub API write-back
# ---------------------------------------------------------------------------


def test_tree_writes_back_api_state_to_db(client, monkeypatch):
    """When _gh_fetch_issue returns a state, it is persisted to task.issue_state."""
    test_client, db_url = client
    guild_id = "tr-wb1"
    # Use default setup: owner IS the test user who has a GitHub token
    insert_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-wb1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-wb1",
        worker_id="w-wb1",
        state="awaiting-review",
        issue_number=7,
        issue_repo="org/repo",
        issue_state=None,  # not yet cached
    )

    # Patch the GitHub API call to return "closed"
    monkeypatch.setattr(
        "routes.tasks._gh_fetch_issue",
        lambda repo, number, token: {"title": "Fix bug", "state": "closed"},
    )

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert nodes[0]["state"] == "closed"

    # DB should now have the state cached
    cached = _get_issue_state(db_url, "task-wb1")
    assert cached == "closed", f"expected 'closed' cached in DB, got {cached!r}"


def test_tree_api_failure_falls_back_to_db_state(client, monkeypatch):
    """When _gh_fetch_issue returns None (API error), the DB-cached state is used."""
    test_client, db_url = client
    guild_id = "tr-apifail1"
    insert_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-apifail1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-apifail1",
        worker_id="w-apifail1",
        state="done",
        issue_number=55,
        issue_repo="org/repo",
        issue_state="closed",
    )

    # Simulate a GitHub API failure
    monkeypatch.setattr("routes.tasks._gh_fetch_issue", lambda repo, number, token: None)

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert nodes[0]["state"] == "closed"


# ---------------------------------------------------------------------------
# Webhook handler — issues event updates issue_state
# ---------------------------------------------------------------------------


def _make_issue_event(repo: str, number: int, action: str) -> bytes:
    payload = {
        "action": action,
        "issue": {
            "number": number,
            "title": "Test issue",
            "state": action if action != "reopened" else "open",
        },
        "repository": {"full_name": repo},
    }
    return json.dumps(payload).encode()


def test_webhook_issues_closed_updates_issue_state(client):
    """A GitHub issues/closed webhook sets issue_state='closed' on linked tasks."""
    test_client, db_url = client
    guild_id = "wh-iss1"
    insert_guild(db_url, guild_id)
    _set_webhook_secret(db_url, guild_id, "wh-secret-1")

    insert_worker(db_url, guild_id, "w-wh-iss1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-wh-iss1",
        worker_id="w-wh-iss1",
        state="done",
        issue_number=100,
        issue_repo="org/repo",
        issue_state="open",
    )

    body = _make_issue_event("org/repo", 100, "closed")
    headers = _signed_headers("wh-secret-1", body, event="issues", delivery="del-iss-001")
    resp = test_client.post(f"/webhooks/github/{guild_id}", content=body, headers=headers)
    assert resp.status_code in (200, 202, 204), resp.text

    assert _get_issue_state(db_url, "task-wh-iss1") == "closed"


def test_webhook_issues_reopened_updates_issue_state(client):
    """A GitHub issues/reopened webhook sets issue_state='open' on linked tasks."""
    test_client, db_url = client
    guild_id = "wh-iss2"
    insert_guild(db_url, guild_id)
    _set_webhook_secret(db_url, guild_id, "wh-secret-2")

    insert_worker(db_url, guild_id, "w-wh-iss2", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-wh-iss2",
        worker_id="w-wh-iss2",
        state="done",
        issue_number=101,
        issue_repo="org/repo",
        issue_state="closed",
    )

    body = _make_issue_event("org/repo", 101, "reopened")
    headers = _signed_headers("wh-secret-2", body, event="issues", delivery="del-iss-002")
    resp = test_client.post(f"/webhooks/github/{guild_id}", content=body, headers=headers)
    assert resp.status_code in (200, 202, 204), resp.text

    assert _get_issue_state(db_url, "task-wh-iss2") == "open"


def test_webhook_issues_only_updates_matching_repo_and_number(client):
    """Issue webhook only updates tasks in the same repo with the same issue number."""
    test_client, db_url = client
    guild_id = "wh-iss3"
    insert_guild(db_url, guild_id)
    _set_webhook_secret(db_url, guild_id, "wh-secret-3")

    insert_worker(db_url, guild_id, "w-wh-iss3", state="online")
    # Matching task
    insert_task(
        db_url,
        guild_id,
        "task-wh-iss3-match",
        worker_id="w-wh-iss3",
        state="done",
        issue_number=200,
        issue_repo="org/repo",
        issue_state="open",
    )
    # Different issue number — must not be updated
    insert_task(
        db_url,
        guild_id,
        "task-wh-iss3-other-num",
        worker_id="w-wh-iss3",
        state="done",
        issue_number=201,
        issue_repo="org/repo",
        issue_state="open",
    )
    # Different repo — must not be updated
    insert_task(
        db_url,
        guild_id,
        "task-wh-iss3-other-repo",
        worker_id="w-wh-iss3",
        state="done",
        issue_number=200,
        issue_repo="org/other-repo",
        issue_state="open",
    )

    body = _make_issue_event("org/repo", 200, "closed")
    headers = _signed_headers("wh-secret-3", body, event="issues", delivery="del-iss-003")
    resp = test_client.post(f"/webhooks/github/{guild_id}", content=body, headers=headers)
    assert resp.status_code in (200, 202, 204), resp.text

    assert _get_issue_state(db_url, "task-wh-iss3-match") == "closed"
    assert _get_issue_state(db_url, "task-wh-iss3-other-num") == "open"
    assert _get_issue_state(db_url, "task-wh-iss3-other-repo") == "open"


# ---------------------------------------------------------------------------
# Issue title — DB fallback and write-back
# ---------------------------------------------------------------------------


def test_tree_returns_cached_title_from_db_when_no_token(client):
    """A task with issue_title cached in the DB returns that title in the tree node."""
    test_client, db_url = client
    guild_id = "tr-title1"
    _guild_no_owner_token(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-title1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-title1",
        worker_id="w-title1",
        state="done",
        issue_number=55,
        issue_repo="org/repo",
        issue_state="closed",
        issue_title="Fix the login bug",
    )

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["title"] == "Fix the login bug"


def test_tree_falls_back_to_issue_number_when_no_title_cached(client):
    """When no issue_title is cached, the node title falls back to '#<number>'."""
    test_client, db_url = client
    guild_id = "tr-title2"
    _guild_no_owner_token(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-title2", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-title2",
        worker_id="w-title2",
        state="pending",
        issue_number=77,
        issue_repo="org/repo",
        issue_state=None,
        issue_title=None,
    )

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["title"] == "#77"


def test_tree_writes_back_api_title_to_db(client, monkeypatch):
    """When _gh_fetch_issue returns a title, it is persisted to task.issue_title."""
    test_client, db_url = client
    guild_id = "tr-wbtitle1"
    insert_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-wbtitle1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-wbtitle1",
        worker_id="w-wbtitle1",
        state="awaiting-review",
        issue_number=8,
        issue_repo="org/repo",
        issue_state=None,
        issue_title=None,
    )

    monkeypatch.setattr(
        "routes.tasks._gh_fetch_issue",
        lambda repo, number, token: {"title": "Add dark mode", "state": "open"},
    )

    headers = _auth(db_url)
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=headers)
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert nodes[0]["title"] == "Add dark mode"

    cached = _get_issue_title(db_url, "task-wbtitle1")
    assert cached == "Add dark mode", f"expected title cached in DB, got {cached!r}"


def test_webhook_issues_edited_updates_issue_title(client):
    """A GitHub issues/edited webhook sets issue_title on linked tasks."""
    test_client, db_url = client
    guild_id = "wh-title1"
    insert_guild(db_url, guild_id)
    _set_webhook_secret(db_url, guild_id, "wh-secret-title1")

    insert_worker(db_url, guild_id, "w-wh-title1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-wh-title1",
        worker_id="w-wh-title1",
        state="pending",
        issue_number=300,
        issue_repo="org/repo",
        issue_state="open",
        issue_title="Old title",
    )

    payload = {
        "action": "edited",
        "issue": {
            "number": 300,
            "title": "New title after edit",
            "state": "open",
        },
        "repository": {"full_name": "org/repo"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("wh-secret-title1", body, event="issues", delivery="del-title-001")
    resp = test_client.post(f"/webhooks/github/{guild_id}", content=body, headers=headers)
    assert resp.status_code in (200, 202, 204), resp.text

    assert _get_issue_title(db_url, "task-wh-title1") == "New title after edit"


def test_webhook_issues_closed_updates_both_state_and_title(client):
    """A GitHub issues/closed webhook sets both issue_state and issue_title."""
    test_client, db_url = client
    guild_id = "wh-both1"
    insert_guild(db_url, guild_id)
    _set_webhook_secret(db_url, guild_id, "wh-secret-both1")

    insert_worker(db_url, guild_id, "w-wh-both1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-wh-both1",
        worker_id="w-wh-both1",
        state="done",
        issue_number=400,
        issue_repo="org/repo",
        issue_state="open",
        issue_title="Fix the thing",
    )

    payload = {
        "action": "closed",
        "issue": {
            "number": 400,
            "title": "Fix the thing (resolved)",
            "state": "closed",
        },
        "repository": {"full_name": "org/repo"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("wh-secret-both1", body, event="issues", delivery="del-both-001")
    resp = test_client.post(f"/webhooks/github/{guild_id}", content=body, headers=headers)
    assert resp.status_code in (200, 202, 204), resp.text

    assert _get_issue_state(db_url, "task-wh-both1") == "closed"
    assert _get_issue_title(db_url, "task-wh-both1") == "Fix the thing (resolved)"
