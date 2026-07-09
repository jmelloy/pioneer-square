"""Tests for the GET /guilds/{guild_id}/tasks/tree endpoint.

The tree groups tasks under coalesce(linked issue, issue its PR closes, its PR),
reading node metadata from the github_issues / github_pull_requests DB caches
with the denormalized issue_title/issue_state task columns as fallback. No
GitHub API calls happen in the request path.

Covers:
- Node metadata from the github_issues cache, and task-column fallback.
- PR-only tasks grouping under a PR node (cache metadata + pr_url parse).
- PR bodies with a closes-reference grouping under the referenced issue.
- Group-key propagation through parent_task_id links, both directions.
- GitHub `issues` webhook events keep issue_state and issue_title current on linked tasks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import (  # noqa: E402
    _sync_session,
    insert_guild,
    insert_member,
    insert_task,
    insert_worker,
    make_auth_token,
)
from models import GithubIssue, GithubPullRequest, Guild, Task  # noqa: E402
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


# ---------------------------------------------------------------------------
# Issue node metadata — github_issues cache, then task-column fallback
# ---------------------------------------------------------------------------


def test_tree_issue_metadata_from_cache(client):
    """Node title/state come from the github_issues cache when a row exists."""
    test_client, db_url = client
    guild_id = "tr-cache1"
    _make_guild(db_url, guild_id)
    _insert_gh_issue(db_url, "org/repo", 42, title="Cached title", state="closed")

    insert_worker(db_url, guild_id, "w-tr1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-tr1",
        worker_id="w-tr1",
        state="done",
        issue_number=42,
        issue_repo="org/repo",
        # Stale denormalized values must lose to the cache row.
        issue_state="open",
        issue_title="Stale title",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["type"] == "issue"
    assert node["number"] == 42
    assert node["repo"] == "org/repo"
    assert node["title"] == "Cached title"
    assert node["state"] == "closed"


def test_tree_falls_back_to_task_columns_without_cache_row(client):
    """Without a github_issues row, denormalized task columns supply title/state."""
    test_client, db_url = client
    guild_id = "tr-fall1"
    _make_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-fall1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-fall1",
        worker_id="w-fall1",
        state="done",
        issue_number=55,
        issue_repo="org/repo",
        issue_state="closed",
        issue_title="Fix the login bug",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["title"] == "Fix the login bug"
    assert nodes[0]["state"] == "closed"


def test_tree_defaults_when_nothing_cached(client):
    """No cache row and no denormalized columns → '#<number>' and 'open'."""
    test_client, db_url = client
    guild_id = "tr-fall2"
    _make_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-fall2", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-fall2",
        worker_id="w-fall2",
        state="pending",
        issue_number=77,
        issue_repo="org/repo",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["title"] == "#77"
    assert nodes[0]["state"] == "open"


def test_tree_open_and_closed_issues_sorted_correctly(client):
    """Open issues appear before closed issues in the response."""
    test_client, db_url = client
    guild_id = "tr-sort1"
    _make_guild(db_url, guild_id)

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

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 2
    assert nodes[0]["state"] == "open"
    assert nodes[1]["state"] == "closed"


# ---------------------------------------------------------------------------
# PR grouping — coalesce(issue, issue closed by PR, PR)
# ---------------------------------------------------------------------------


def test_tree_pr_only_task_groups_under_pr_node(client):
    """A task with only PR linkage groups under a type='pr' node with cache metadata."""
    test_client, db_url = client
    guild_id = "tr-pr1"
    _make_guild(db_url, guild_id)
    _insert_gh_pr(db_url, "org/repo", 300, title="Add dark mode", state="merged", merged=True)

    insert_worker(db_url, guild_id, "w-pr1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-pr1",
        worker_id="w-pr1",
        state="done",
        phase="review",
        pr_number=300,
        pr_repo="org/repo",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["type"] == "pr"
    assert node["number"] == 300
    assert node["title"] == "Add dark mode"
    assert node["state"] == "merged"


def test_tree_pr_closes_ref_groups_under_issue(client):
    """A PR whose cached body closes an issue groups its task under that issue."""
    test_client, db_url = client
    guild_id = "tr-pr2"
    _make_guild(db_url, guild_id)
    _insert_gh_pr(db_url, "org/repo", 301, title="Fix crash", body="Fixes #12")
    _insert_gh_issue(db_url, "org/repo", 12, title="Crash on save", state="open")

    insert_worker(db_url, guild_id, "w-pr2", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-pr2",
        worker_id="w-pr2",
        state="working",
        pr_number=301,
        pr_repo="org/repo",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["type"] == "issue"
    assert node["number"] == 12
    assert node["title"] == "Crash on save"


def test_tree_pr_url_parsed_when_no_pr_columns(client):
    """pr_url is parsed for the PR key when pr_repo/pr_number are unset."""
    test_client, db_url = client
    guild_id = "tr-pr3"
    _make_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-pr3", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-pr3",
        worker_id="w-pr3",
        state="done",
        pr_url="https://github.com/org/repo/pull/302",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["type"] == "pr"
    assert node["number"] == 302
    assert node["title"] == "PR #302"  # no cache row → fallback title
    assert node["state"] == "open"


# ---------------------------------------------------------------------------
# Group-key propagation through parent links (issue #861 lineage)
# ---------------------------------------------------------------------------


def test_tree_nests_execute_task_under_issue_root_via_parent_task_id(client):
    """A phase='issue' root with a child (parent_task_id set) renders one root."""
    test_client, db_url = client
    guild_id = "tr-issroot1"
    _make_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-issroot1", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-issroot1-root",
        phase="issue",
        state="pending",
        issue_number=61,
        issue_repo="org/repo",
        issue_state="open",
    )
    insert_task(
        db_url,
        guild_id,
        "task-issroot1-exec",
        worker_id="w-issroot1",
        phase="execute",
        state="working",
        issue_number=61,
        issue_repo="org/repo",
        issue_state="open",
        parent_task_id="task-issroot1-root",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    tasks = nodes[0]["tasks"]
    assert len(tasks) == 1, f"expected a single root task, got {len(tasks)}: {tasks}"
    assert tasks[0]["id"] == "task-issroot1-root"
    assert tasks[0]["phase"] == "issue"
    assert len(tasks[0]["children"]) == 1
    assert tasks[0]["children"][0]["id"] == "task-issroot1-exec"


def test_tree_nests_orphan_task_under_issue_root_when_parent_unset(client):
    """A task in the same issue group without parent_task_id is still nested
    under the phase='issue' root rather than appearing as a second root."""
    test_client, db_url = client
    guild_id = "tr-issroot2"
    _make_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-issroot2", state="online")
    insert_task(
        db_url,
        guild_id,
        "task-issroot2-root",
        phase="issue",
        state="pending",
        issue_number=62,
        issue_repo="org/repo",
        issue_state="open",
    )
    # parent_task_id intentionally left unset — this is the bug scenario.
    insert_task(
        db_url,
        guild_id,
        "task-issroot2-exec",
        worker_id="w-issroot2",
        phase="execute",
        state="working",
        issue_number=62,
        issue_repo="org/repo",
        issue_state="open",
    )

    nodes = _get_tree(test_client, db_url, guild_id)["nodes"]
    assert len(nodes) == 1
    tasks = nodes[0]["tasks"]
    assert len(tasks) == 1, f"expected a single root task, got {len(tasks)}: {tasks}"
    assert tasks[0]["id"] == "task-issroot2-root"
    child_ids = {c["id"] for c in tasks[0]["children"]}
    assert child_ids == {"task-issroot2-exec"}


def test_tree_unlinked_root_joins_childrens_group(client):
    """A legacy phase='issue' root with NO issue linkage of its own inherits the
    group key upward from its linked children instead of landing in ungrouped."""
    test_client, db_url = client
    guild_id = "tr-upprop1"
    _make_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-upprop1", state="online")
    # Legacy root: no issue_number/issue_repo (pre-linkage create_task).
    insert_task(
        db_url,
        guild_id,
        "task-upprop1-root",
        phase="issue",
        state="pending",
        description="Issue root for #63",
    )
    insert_task(
        db_url,
        guild_id,
        "task-upprop1-exec",
        worker_id="w-upprop1",
        phase="execute",
        state="working",
        issue_number=63,
        issue_repo="org/repo",
        parent_task_id="task-upprop1-root",
    )

    data = _get_tree(test_client, db_url, guild_id)
    assert data["ungrouped"] == []
    nodes = data["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["number"] == 63
    tasks = nodes[0]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["id"] == "task-upprop1-root"
    assert tasks[0]["children"][0]["id"] == "task-upprop1-exec"


def test_tree_unlinked_task_stays_ungrouped(client):
    """A task with no linkage anywhere in its tree lands in ungrouped."""
    test_client, db_url = client
    guild_id = "tr-ungr1"
    _make_guild(db_url, guild_id)

    insert_worker(db_url, guild_id, "w-ungr1", state="online")
    insert_task(db_url, guild_id, "task-ungr1", worker_id="w-ungr1", state="working")

    data = _get_tree(test_client, db_url, guild_id)
    assert data["nodes"] == []
    assert [t["id"] for t in data["ungrouped"]] == ["task-ungr1"]


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
