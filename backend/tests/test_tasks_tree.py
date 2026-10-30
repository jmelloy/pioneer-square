"""Tests for the GET /guilds/{guild_id}/tasks/tree endpoint.

The tree groups tasks by conversation_id, propagated through parent_task_id
links in both directions; conversation metadata comes from the conversations table.

Covers:
- Grouping, propagation, sorting and ungrouped handling.
- conversation_id on the flat task list.
- GitHub `issues` webhook events keep issue_state and issue_title current on linked tasks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import (  # noqa: E402
    _sync_session,
    insert_conversation,
    insert_guild,
    insert_member,
    insert_task,
    insert_worker,
    make_auth_token,
)
from models import Conversation, GithubIssue, GithubPullRequest, Guild, Task  # noqa: E402
from sqlalchemy import insert, select, update  # noqa: E402
from sqlmodel import col  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(db_url: str, user_id: str = "gh-user-test") -> dict:
    token = make_auth_token(db_url, user_id=user_id, username=user_id)
    return {"Authorization": f"Bearer {token}"}


def _make_guild(db_url: str, guild_id: str) -> None:
    insert_guild(db_url, guild_id, owner_user_id="owner-notokenuser")
    insert_member(db_url, guild_id, "gh-user-test", role="member")


def _insert_gh_issue(
    db_url: str,
    repo: str,
    number: int,
    *,
    title: str = "",
    state: str = "open",
    body: str | None = None,
) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            insert(GithubIssue).values(
                repo=repo,
                number=number,
                title=title,
                state=state,
                body=body,
                assignees=[],
                labels=[],
                created_at=now,
                updated_at=now,
                raw={},
            )
        )
        session.commit()


def _insert_gh_pr(
    db_url: str,
    repo: str,
    number: int,
    *,
    title: str = "",
    state: str = "open",
    body: str | None = None,
    merged: bool = False,
) -> None:
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            insert(GithubPullRequest).values(
                repo=repo,
                number=number,
                title=title,
                state=state,
                body=body,
                merged=merged,
                draft=False,
                assignees=[],
                labels=[],
                created_at=now,
                updated_at=now,
                raw={},
            )
        )
        session.commit()


def _get_issue_state(db_url: str, task_id: str) -> str | None:
    with _sync_session(db_url) as session:
        row = session.execute(
            select(col(Task.issue_state)).where(col(Task.id) == task_id)
        ).one_or_none()
    return row[0] if row else None


def _get_issue_title(db_url: str, task_id: str) -> str | None:
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


def _get_tree(test_client, db_url: str, guild_id: str) -> dict:
    resp = test_client.get(f"/guilds/{guild_id}/tasks/tree", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _set_conv(db_url: str, conv_id: int, **values) -> None:
    with _sync_session(db_url) as session:
        session.execute(
            update(Conversation).where(col(Conversation.id) == conv_id).values(**values)
        )
        session.commit()


# ---------------------------------------------------------------------------
# Conversation grouping
# ---------------------------------------------------------------------------


def test_tree_groups_tasks_by_conversation(client):
    test_client, db_url = client
    guild_id = "tr-conv1"
    _make_guild(db_url, guild_id)
    insert_worker(db_url, guild_id, "w-conv1", state="online")
    conv = insert_conversation(db_url, guild_id)
    _set_conv(db_url, conv, name="My thread")
    insert_task(db_url, guild_id, "task-c1", worker_id="w-conv1", conversation_id=conv)
    insert_task(db_url, guild_id, "task-c2", worker_id="w-conv1", conversation_id=conv)

    data = _get_tree(test_client, db_url, guild_id)
    assert "nodes" not in data
    assert data["ungrouped"] == []
    [c] = data["conversations"]
    assert c["id"] == conv
    assert c["name"] == "My thread"
    assert c["status"] == "active"
    assert c["created_at"] and c["updated_at"]
    assert {t["id"] for t in c["tasks"]} == {"task-c1", "task-c2"}


def test_tree_propagates_conversation_through_parent_links(client):
    """Tasks without a conversation join their parent's/children's conversation."""
    test_client, db_url = client
    guild_id = "tr-conv2"
    _make_guild(db_url, guild_id)
    insert_worker(db_url, guild_id, "w-conv2", state="online")
    conv = insert_conversation(db_url, guild_id)
    # Unlinked root with a linked child (upward), plus an unlinked grandchild (downward).
    insert_task(db_url, guild_id, "task-root", worker_id="w-conv2", phase="issue")
    insert_task(
        db_url,
        guild_id,
        "task-mid",
        worker_id="w-conv2",
        parent_task_id="task-root",
        conversation_id=conv,
    )
    insert_task(db_url, guild_id, "task-leaf", worker_id="w-conv2", parent_task_id="task-mid")

    data = _get_tree(test_client, db_url, guild_id)
    assert data["ungrouped"] == []
    [c] = data["conversations"]
    [root] = c["tasks"]
    assert root["id"] == "task-root"
    assert root["conversation_id"] == conv
    [mid] = root["children"]
    assert mid["id"] == "task-mid"
    assert [t["id"] for t in mid["children"]] == ["task-leaf"]
    assert mid["children"][0]["conversation_id"] == conv


def test_tree_sorts_active_with_live_tasks_first_then_recent(client):
    test_client, db_url = client
    guild_id = "tr-conv3"
    _make_guild(db_url, guild_id)
    insert_worker(db_url, guild_id, "w-conv3", state="online")
    now = datetime.now(UTC)
    old_live = insert_conversation(db_url, guild_id)
    new_done = insert_conversation(db_url, guild_id)
    newer_done = insert_conversation(db_url, guild_id)
    archived_live = insert_conversation(db_url, guild_id)
    _set_conv(db_url, old_live, updated_at=now - timedelta(days=5))
    _set_conv(db_url, new_done, updated_at=now - timedelta(hours=2))
    _set_conv(db_url, newer_done, updated_at=now - timedelta(hours=1))
    _set_conv(db_url, archived_live, updated_at=now, status="archived")
    for i, (conv, state) in enumerate(
        [
            (old_live, "working"),
            (new_done, "done"),
            (newer_done, "done"),
            (archived_live, "working"),
        ]
    ):
        insert_task(
            db_url, guild_id, f"task-s{i}", worker_id="w-conv3", state=state, conversation_id=conv
        )

    data = _get_tree(test_client, db_url, guild_id)
    assert [c["id"] for c in data["conversations"]] == [
        old_live,
        archived_live,
        newer_done,
        new_done,
    ]


def test_tree_unlinked_task_stays_ungrouped(client):
    test_client, db_url = client
    guild_id = "tr-ungr1"
    _make_guild(db_url, guild_id)
    insert_worker(db_url, guild_id, "w-ungr1", state="online")
    insert_task(db_url, guild_id, "task-ungr1", worker_id="w-ungr1", state="working")

    data = _get_tree(test_client, db_url, guild_id)
    assert data["conversations"] == []
    assert [t["id"] for t in data["ungrouped"]] == ["task-ungr1"]


def test_list_tasks_includes_conversation_id(client):
    test_client, db_url = client
    guild_id = "tr-list1"
    _make_guild(db_url, guild_id)
    insert_worker(db_url, guild_id, "w-list1", state="online")
    conv = insert_conversation(db_url, guild_id)
    insert_task(db_url, guild_id, "task-l1", worker_id="w-list1", conversation_id=conv)
    insert_task(db_url, guild_id, "task-l2", worker_id="w-list1")

    resp = test_client.get(f"/guilds/{guild_id}/tasks", headers=_auth(db_url))
    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()}
    assert by_id["task-l1"]["conversation_id"] == conv
    assert by_id["task-l2"]["conversation_id"] is None


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
