"""Tests for the GitHub webhook receiver and webhook-secret endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, make_auth_token, raw_conn


def _set_webhook_secret(db_url: str, guild_id: str, secret: str) -> None:
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "UPDATE guilds SET webhook_secret = %s WHERE guild_id = %s",
            (secret, guild_id),
        )


def _insert_task(
    db_url: str,
    *,
    task_id: str,
    guild_id: str,
    pr_url: str | None = None,
    pr_number: int | None = None,
    pr_repo: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with raw_conn(db_url) as (conn, cur):
        cur.execute("SELECT id FROM guilds WHERE guild_id = %s AND deleted_at IS NULL", (guild_id,))
        row = cur.fetchone()
        guild_id = row["id"]
        # Workers FK is NOT NULL; insert a placeholder worker row first.
        cur.execute(
            "INSERT INTO workers (id, guild_id, repos, state, created_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (f"w-{task_id}", guild_id, "[]", "online", now),
        )
        cur.execute(
            "INSERT INTO tasks (id, worker_id, guild_id, description, tool, state, "
            "pr_url, pr_number, pr_repo, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                task_id,
                f"w-{task_id}",
                guild_id,
                "test task",
                "claude",
                "awaiting-review",
                pr_url,
                pr_number,
                pr_repo,
                now,
            ),
        )


def _signed_headers(secret: str, body: bytes, *, event: str, delivery: str) -> dict[str, str]:
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={sig}",
        "Content-Type": "application/json",
    }


def _pr_payload(*, action: str, repo: str, number: int) -> dict:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "html_url": f"https://github.com/{repo}/pull/{number}",
        },
        "sender": {"login": "octocat"},
    }


# ---------------------------------------------------------------------------
# Webhook receiver
# ---------------------------------------------------------------------------


def test_webhook_rejects_unknown_guild(client):
    test_client, _ = client
    body = json.dumps({}).encode()
    resp = test_client.post(
        "/webhooks/github/nosuch",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "abc",
            "X-Hub-Signature-256": "sha256=00",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 404


def test_webhook_rejects_guild_without_secret(client):
    test_client, db_url = client
    insert_guild(db_url, "g1")
    body = json.dumps({}).encode()
    resp = test_client.post(
        "/webhooks/github/g1",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-GitHub-Delivery": "abc",
            "X-Hub-Signature-256": "sha256=00",
            "Content-Type": "application/json",
        },
    )
    # Webhook secret never generated for this guild
    assert resp.status_code == 404


def test_webhook_rejects_bad_signature(client):
    test_client, db_url = client
    insert_guild(db_url, "g2")
    _set_webhook_secret(db_url, "g2", "topsecret")
    body = json.dumps({"action": "opened"}).encode()
    resp = test_client.post(
        "/webhooks/github/g2",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-1",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_webhook_rejects_missing_signature(client):
    test_client, db_url = client
    insert_guild(db_url, "g3")
    _set_webhook_secret(db_url, "g3", "topsecret")
    body = json.dumps({"action": "opened"}).encode()
    resp = test_client.post(
        "/webhooks/github/g3",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "d-1",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_webhook_accepts_ping(client):
    test_client, db_url = client
    insert_guild(db_url, "g4")
    secret = "topsecret"
    _set_webhook_secret(db_url, "g4", secret)
    body = json.dumps({"zen": "Mind your words"}).encode()
    headers = _signed_headers(secret, body, event="ping", delivery="d-ping")
    resp = test_client.post("/webhooks/github/g4", content=body, headers=headers)
    assert resp.status_code == 204
    # Ping deliveries are not persisted
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "SELECT COUNT(*) FROM github_events"
            " WHERE guild_id = (SELECT id FROM guilds WHERE guild_id = 'g4' AND deleted_at IS NULL)"
        )
        rows = cur.fetchone()
    assert rows["count"] == 0


def test_webhook_persists_event_and_links_task(client):
    test_client, db_url = client
    insert_guild(db_url, "g5")
    _set_webhook_secret(db_url, "g5", "s5")
    _insert_task(
        db_url,
        task_id="t-1",
        guild_id="g5",
        pr_url="https://github.com/owner/repo/pull/42",
        pr_number=42,
        pr_repo="owner/repo",
    )
    payload = _pr_payload(action="opened", repo="owner/repo", number=42)
    body = json.dumps(payload).encode()
    headers = _signed_headers("s5", body, event="pull_request", delivery="d-evt-1")
    resp = test_client.post("/webhooks/github/g5", content=body, headers=headers)
    assert resp.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "SELECT task_id, delivery_id, event_type, action, repo, "
            "pr_number, pr_url, sender_login FROM github_events"
            " WHERE delivery_id = 'd-evt-1'"
        )
        row = cur.fetchone()
    assert row["task_id"] == "t-1"
    assert row["delivery_id"] == "d-evt-1"
    assert row["event_type"] == "pull_request"
    assert row["action"] == "opened"
    assert row["repo"] == "owner/repo"
    assert row["pr_number"] == 42
    assert row["pr_url"] == "https://github.com/owner/repo/pull/42"
    assert row["sender_login"] == "octocat"


def test_webhook_event_without_matching_task(client):
    test_client, db_url = client
    insert_guild(db_url, "g6")
    _set_webhook_secret(db_url, "g6", "s6")
    payload = _pr_payload(action="opened", repo="owner/repo", number=7)
    body = json.dumps(payload).encode()
    headers = _signed_headers("s6", body, event="pull_request", delivery="d-evt-2")
    resp = test_client.post("/webhooks/github/g6", content=body, headers=headers)
    assert resp.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute("SELECT task_id, pr_number FROM github_events WHERE delivery_id = 'd-evt-2'")
        row = cur.fetchone()
    assert row["task_id"] is None
    assert row["pr_number"] == 7


def test_webhook_dedupes_redelivery(client):
    test_client, db_url = client
    insert_guild(db_url, "g7")
    _set_webhook_secret(db_url, "g7", "s7")
    payload = _pr_payload(action="opened", repo="owner/repo", number=1)
    body = json.dumps(payload).encode()
    headers = _signed_headers("s7", body, event="pull_request", delivery="dup-1")
    r1 = test_client.post("/webhooks/github/g7", content=body, headers=headers)
    r2 = test_client.post("/webhooks/github/g7", content=body, headers=headers)
    assert r1.status_code == 202
    assert r2.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute("SELECT COUNT(*) FROM github_events WHERE delivery_id = 'dup-1'")
        n = cur.fetchone()["count"]
    assert n == 1


def test_webhook_check_run_extracts_pr_from_pull_requests_array(client):
    test_client, db_url = client
    insert_guild(db_url, "g8")
    _set_webhook_secret(db_url, "g8", "s8")
    _insert_task(
        db_url,
        task_id="t-2",
        guild_id="g8",
        pr_url="https://github.com/o/r/pull/9",
        pr_number=9,
        pr_repo="o/r",
    )
    payload = {
        "action": "completed",
        "repository": {"full_name": "o/r"},
        "check_run": {
            "name": "rspec",
            "conclusion": "failure",
            "pull_requests": [{"number": 9, "html_url": "https://github.com/o/r/pull/9"}],
        },
        "sender": {"login": "ci-bot[bot]"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("s8", body, event="check_run", delivery="cr-1")
    resp = test_client.post("/webhooks/github/g8", content=body, headers=headers)
    assert resp.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "SELECT task_id, event_type, action, pr_number FROM github_events"
            " WHERE delivery_id = 'cr-1'"
        )
        row = cur.fetchone()
    assert row["task_id"] == "t-2"
    assert row["event_type"] == "check_run"
    assert row["action"] == "completed"
    assert row["pr_number"] == 9


def test_webhook_rejects_invalid_json(client):
    test_client, db_url = client
    insert_guild(db_url, "g9")
    _set_webhook_secret(db_url, "g9", "s9")
    body = b"not-json"
    headers = _signed_headers("s9", body, event="pull_request", delivery="bad-1")
    resp = test_client.post("/webhooks/github/g9", content=body, headers=headers)
    assert resp.status_code == 400


def test_webhook_missing_github_headers(client):
    test_client, db_url = client
    insert_guild(db_url, "g10")
    _set_webhook_secret(db_url, "g10", "s10")
    body = json.dumps({}).encode()
    resp = test_client.post(
        "/webhooks/github/g10",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_webhook_emits_foreman_chat_message(client):
    test_client, db_url = client
    insert_guild(db_url, "gchat1")
    _set_webhook_secret(db_url, "gchat1", "schat1")
    _insert_task(
        db_url,
        task_id="t-chat1",
        guild_id="gchat1",
        pr_url="https://github.com/owner/repo/pull/10",
        pr_number=10,
        pr_repo="owner/repo",
    )
    payload = _pr_payload(action="opened", repo="owner/repo", number=10)
    body = json.dumps(payload).encode()
    headers = _signed_headers("schat1", body, event="pull_request", delivery="d-chat-1")
    resp = test_client.post("/webhooks/github/gchat1", content=body, headers=headers)
    assert resp.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "SELECT from_agent, to_agent, content, message_type FROM messages"
            " WHERE guild_id = (SELECT id FROM guilds WHERE guild_id = 'gchat1' AND deleted_at IS NULL)"
        )
        row = cur.fetchone()
    assert row is not None
    assert row["from_agent"] == "github"
    assert row["to_agent"] == "foreman"
    assert row["content"].startswith("[github-event]")
    assert "pull_request/opened" in row["content"]
    assert "owner/repo#10" in row["content"]
    assert "task: t-chat1" in row["content"]
    assert row["message_type"] == "chat"


def test_webhook_chat_line_includes_merged_status(client):
    test_client, db_url = client
    insert_guild(db_url, "gchat2")
    _set_webhook_secret(db_url, "gchat2", "schat2")
    payload = {
        "action": "closed",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 5,
            "html_url": "https://github.com/owner/repo/pull/5",
            "merged": True,
        },
        "sender": {"login": "octocat"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("schat2", body, event="pull_request", delivery="d-chat-2")
    resp = test_client.post("/webhooks/github/gchat2", content=body, headers=headers)
    assert resp.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "SELECT content FROM messages"
            " WHERE guild_id = (SELECT id FROM guilds WHERE guild_id = 'gchat2' AND deleted_at IS NULL)"
        )
        row = cur.fetchone()
    assert row is not None
    assert "merged=true" in row["content"]
    assert "task:" not in row["content"]  # no task linked


def test_webhook_chat_line_not_emitted_for_duplicate(client):
    test_client, db_url = client
    insert_guild(db_url, "gchat3")
    _set_webhook_secret(db_url, "gchat3", "schat3")
    payload = _pr_payload(action="opened", repo="owner/repo", number=1)
    body = json.dumps(payload).encode()
    headers = _signed_headers("schat3", body, event="pull_request", delivery="d-chat-dup")
    test_client.post("/webhooks/github/gchat3", content=body, headers=headers)
    test_client.post("/webhooks/github/gchat3", content=body, headers=headers)
    with raw_conn(db_url) as (conn, cur):
        cur.execute(
            "SELECT COUNT(*) FROM messages"
            " WHERE guild_id = (SELECT id FROM guilds WHERE guild_id = 'gchat3' AND deleted_at IS NULL)"
        )
        count = cur.fetchone()["count"]
    assert count == 1  # second delivery is a duplicate; no second message


# ---------------------------------------------------------------------------
# Webhook-secret REST endpoints
# ---------------------------------------------------------------------------


def test_get_webhook_secret_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "gs1")
    resp = test_client.get("/guilds/gs1/webhook-secret")
    assert resp.status_code == 401


def test_get_webhook_secret_generates_on_first_access(client):
    test_client, db_url = client
    insert_guild(db_url, "gs2")
    token = make_auth_token(db_url)
    resp = test_client.get(
        "/guilds/gs2/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    secret = resp.json()["webhook_secret"]
    assert secret and len(secret) == 64  # 32 bytes hex

    # Same secret on second access (no rotation)
    resp2 = test_client.get(
        "/guilds/gs2/webhook-secret",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.json()["webhook_secret"] == secret


def test_rotate_webhook_secret_owner_only(client):
    test_client, db_url = client
    insert_guild(db_url, "gs3")
    other = make_auth_token(db_url, user_id="someone-else", username="other")
    resp = test_client.post(
        "/guilds/gs3/webhook-secret",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 403


def test_rotate_webhook_secret_changes_value(client):
    test_client, db_url = client
    insert_guild(db_url, "gs4")
    token = make_auth_token(db_url)
    headers = {"Authorization": f"Bearer {token}"}
    first = test_client.get("/guilds/gs4/webhook-secret", headers=headers).json()["webhook_secret"]
    rotated = test_client.post("/guilds/gs4/webhook-secret", headers=headers).json()[
        "webhook_secret"
    ]
    assert rotated != first


# ---------------------------------------------------------------------------
# pr_url -> (number, repo) backfill
# ---------------------------------------------------------------------------


def test_parse_pr_url_web():
    from ws_handlers import _parse_pr_url

    assert _parse_pr_url("https://github.com/owner/repo/pull/42") == (42, "owner/repo")


def test_parse_pr_url_api():
    from ws_handlers import _parse_pr_url

    assert _parse_pr_url("https://api.github.com/repos/o/r/pulls/9") == (9, "o/r")


def test_parse_pr_url_handles_garbage():
    from ws_handlers import _parse_pr_url

    assert _parse_pr_url(None) == (None, None)
    assert _parse_pr_url("") == (None, None)
    assert _parse_pr_url("not-a-url") == (None, None)
    assert _parse_pr_url("https://github.com/owner/repo/issues/1") == (None, None)


# ---------------------------------------------------------------------------
# Webhook back-fills Task.pr_url when it was never set by the worker
# ---------------------------------------------------------------------------


def test_webhook_backfills_task_pr_url(client):
    """pull_request event sets Task.pr_url when the task has pr_number/pr_repo but no pr_url."""
    test_client, db_url = client
    insert_guild(db_url, "gbf1")
    _set_webhook_secret(db_url, "gbf1", "sbf1")
    # Task has pr_number + pr_repo (set when the branch was pushed) but pr_url is NULL.
    _insert_task(
        db_url,
        task_id="t-bf1",
        guild_id="gbf1",
        pr_url=None,
        pr_number=55,
        pr_repo="org/my-repo",
    )
    payload = {
        "action": "opened",
        "repository": {"full_name": "org/my-repo"},
        "pull_request": {
            "number": 55,
            "html_url": "https://github.com/org/my-repo/pull/55",
        },
        "sender": {"login": "octocat"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("sbf1", body, event="pull_request", delivery="bf-1")
    resp = test_client.post("/webhooks/github/gbf1", content=body, headers=headers)
    assert resp.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute("SELECT pr_url FROM tasks WHERE id = 't-bf1'")
        row = cur.fetchone()
    assert row is not None
    assert row["pr_url"] == "https://github.com/org/my-repo/pull/55"


def test_webhook_does_not_overwrite_existing_pr_url(client):
    """Webhook must not clobber a pr_url that was already set by the worker."""
    test_client, db_url = client
    insert_guild(db_url, "gbf2")
    _set_webhook_secret(db_url, "gbf2", "sbf2")
    _insert_task(
        db_url,
        task_id="t-bf2",
        guild_id="gbf2",
        pr_url="https://github.com/org/my-repo/pull/77",
        pr_number=77,
        pr_repo="org/my-repo",
    )
    payload = {
        "action": "synchronize",
        "repository": {"full_name": "org/my-repo"},
        "pull_request": {
            "number": 77,
            "html_url": "https://github.com/org/my-repo/pull/77",
        },
        "sender": {"login": "octocat"},
    }
    body = json.dumps(payload).encode()
    headers = _signed_headers("sbf2", body, event="pull_request", delivery="bf-2")
    resp = test_client.post("/webhooks/github/gbf2", content=body, headers=headers)
    assert resp.status_code == 202
    with raw_conn(db_url) as (conn, cur):
        cur.execute("SELECT pr_url FROM tasks WHERE id = 't-bf2'")
        row = cur.fetchone()
    assert row is not None
    assert row["pr_url"] == "https://github.com/org/my-repo/pull/77"
