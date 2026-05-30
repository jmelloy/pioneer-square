"""Tests for the Claude usage reporting routes (routes/usage.py).

Requires the postgres-test container (localhost:5433); see CLAUDE.md.
"""

from __future__ import annotations

import events as events_module
from helpers import insert_guild, insert_task, insert_worker, make_auth_token


def _auth(db_url: str) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_url)}"}


def _sample_records() -> list[dict]:
    return [
        {
            "kind": "api_call",
            "call_index": 0,
            "model": "claude-opus-4-8",
            "input_tokens": 5,
            "output_tokens": 7,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 2,
        },
        {
            "kind": "api_call",
            "call_index": 1,
            "model": "claude-opus-4-8",
            "input_tokens": 3,
            "output_tokens": 4,
        },
        {
            "kind": "result",
            "model": "claude-opus-4-8",
            "input_tokens": 100,
            "output_tokens": 11,
            "cost_usd": 0.5,
            "num_turns": 2,
            "stop_reason": "success",
        },
    ]


def test_report_usage_stores_rows_and_broadcasts(client, monkeypatch):
    test_client, db_url = client
    insert_guild(db_url, "guseu1")
    insert_worker(db_url, "guseu1", "w-use01")
    insert_task(db_url, "guseu1", "t-use01", worker_id="w-use01")

    captured: list[tuple[str, dict]] = []

    async def _fake_broadcast(guild_id, message, exclude=None):
        captured.append((guild_id, message))

    monkeypatch.setattr(events_module, "broadcast", _fake_broadcast)
    # routes.usage imported broadcast by name, so patch it there too.
    import routes.usage as usage_module

    monkeypatch.setattr(usage_module, "broadcast", _fake_broadcast)

    resp = test_client.post(
        "/guilds/guseu1/usage",
        json={
            "task_id": "t-use01",
            "worker_id": "w-use01",
            "session_id": "sess-1",
            "repo": "owner/repo",
            "reporter": "tok/w-use01",
            "records": _sample_records(),
        },
        headers=_auth(db_url),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"stored": 3}

    # Broadcast carried the summed-vs-reported discrepancy.
    assert len(captured) == 1
    _, msg = captured[0]
    assert msg["type"] == "claude-usage"
    assert msg["taskId"] == "t-use01"
    assert msg["apiCalls"] == 2
    assert msg["summedInputTokens"] == 8
    assert msg["reportedInputTokens"] == 100
    assert msg["costUsd"] == 0.5
    assert msg["stopReason"] == "success"


def test_list_usage_returns_rows(client):
    test_client, db_url = client
    insert_guild(db_url, "guseu2")
    insert_worker(db_url, "guseu2", "w-use02")
    insert_task(db_url, "guseu2", "t-use02", worker_id="w-use02")

    post = test_client.post(
        "/guilds/guseu2/usage",
        json={"task_id": "t-use02", "worker_id": "w-use02", "records": _sample_records()},
        headers=_auth(db_url),
    )
    assert post.status_code == 200, post.text

    resp = test_client.get("/guilds/guseu2/usage", headers=_auth(db_url))
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 3
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["api_call", "api_call", "result"]
    result_row = next(r for r in rows if r["kind"] == "result")
    assert result_row["cost_usd"] == 0.5
    assert result_row["num_turns"] == 2

    # Filtering by an unrelated task returns nothing.
    empty = test_client.get("/guilds/guseu2/usage?task_id=t-nope", headers=_auth(db_url))
    assert empty.status_code == 200
    assert empty.json() == []


def test_report_usage_unknown_guild_404(client):
    test_client, db_url = client
    resp = test_client.post(
        "/guilds/nope01/usage",
        json={"task_id": "t-x", "records": _sample_records()},
        headers=_auth(db_url),
    )
    assert resp.status_code == 404


def test_report_usage_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "guseu3")
    resp = test_client.post(
        "/guilds/guseu3/usage",
        json={"task_id": "t-x", "records": _sample_records()},
    )
    assert resp.status_code == 401


def test_report_usage_empty_records_noop(client):
    test_client, db_url = client
    insert_guild(db_url, "guseu4")
    resp = test_client.post(
        "/guilds/guseu4/usage",
        json={"task_id": "t-x", "records": []},
        headers=_auth(db_url),
    )
    assert resp.status_code == 200
    assert resp.json() == {"stored": 0}
