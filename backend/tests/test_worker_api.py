"""Tests for worker and task REST endpoints."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, make_auth_token


def _auth(db_path: str) -> dict:
    """Helper: return Authorization header for the default test user."""
    return {"Authorization": f"Bearer {make_auth_token(db_path)}"}


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def test_list_workers_empty(client):
    test_client, db_path = client
    insert_guild(db_path, "guild01")
    resp = test_client.get("/guilds/guild01/workers", headers=_auth(db_path))
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_worker_returns_id(client):
    test_client, db_path = client
    insert_guild(db_path, "guild02")
    resp = test_client.post(
        "/guilds/guild02/workers",
        json={"repos": ["owner/repo-a"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"].startswith("w-")
    assert data["repos"] == ["owner/repo-a"]
    assert "name" in data


def test_create_worker_appears_in_list(client):
    test_client, db_path = client
    insert_guild(db_path, "guild03")
    test_client.post("/guilds/guild03/workers", json={"repos": ["org/backend"]})
    resp = test_client.get("/guilds/guild03/workers", headers=_auth(db_path))
    assert resp.status_code == 200
    workers = resp.json()
    assert len(workers) == 1
    assert isinstance(workers[0]["guild_pk"], int)


def test_create_worker_does_not_insert_agent_row(client):
    """Worker registration must not create a phantom agent row (issue #264).

    Agent rows are created only when the worker process sends a ``join``
    message over WebSocket, not during REST-based registration.
    """
    import sqlite3

    test_client, db_path = client
    insert_guild(db_path, "guild03b")
    resp = test_client.post("/guilds/guild03b/workers", json={"repos": []})
    assert resp.status_code == 200
    worker_id = resp.json()["id"]

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT id FROM agents WHERE id = ?", (worker_id,)).fetchone()
    assert row is None, f"create_worker must not insert an agent row, but found: {row}"


def test_create_multiple_workers(client):
    test_client, db_path = client
    insert_guild(db_path, "guild04")
    test_client.post("/guilds/guild04/workers", json={"repos": []})
    test_client.post("/guilds/guild04/workers", json={"repos": ["x/y"]})
    resp = test_client.get("/guilds/guild04/workers", headers=_auth(db_path))
    assert len(resp.json()) == 2


def test_create_worker_attributes_to_user(client):
    """Worker registration with `user` resolves to a users.id and stores it on workers.user_id."""
    import sqlite3

    from helpers import insert_member

    test_client, db_path = client
    insert_guild(db_path, "guildusr")
    insert_member(db_path, "guildusr", "user-bob", role="member")
    # Bob's users row was created by insert_member; reference by login should also work.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET github_login = ? WHERE id = ?",
            ("bobby", "user-bob"),
        )
        conn.commit()
    resp = test_client.post(
        "/guilds/guildusr/workers",
        json={"repos": [], "user": "bobby"},
    )
    assert resp.status_code == 200
    worker_id = resp.json()["id"]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT user_id FROM workers WHERE id = ?", (worker_id,)).fetchone()
    assert row[0] == "user-bob"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _create_worker(test_client, guild_id: str) -> str:
    resp = test_client.post(f"/guilds/{guild_id}/workers", json={"repos": []})
    return resp.json()["id"]


def test_list_tasks_empty(client):
    test_client, db_path = client
    insert_guild(db_path, "guild05")
    worker_id = _create_worker(test_client, "guild05")
    resp = test_client.get(f"/guilds/guild05/workers/{worker_id}/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_assign_task_unknown_worker(client):
    test_client, db_path = client
    insert_guild(db_path, "guild06")
    resp = test_client.post(
        "/guilds/guild06/workers/w-nosuch/tasks",
        json={"description": "do something", "tool": "claude"},
        headers=_auth(db_path),
    )
    assert resp.status_code == 404


def test_assign_task_returns_pending(client):
    test_client, db_path = client
    insert_guild(db_path, "guild07")
    worker_id = _create_worker(test_client, "guild07")

    resp = test_client.post(
        f"/guilds/guild07/workers/{worker_id}/tasks",
        json={"description": "Write a hello-world script", "tool": "claude"},
        headers=_auth(db_path),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "pending"
    assert data["worker_id"] == worker_id
    assert data["id"].startswith("t-")


def test_assign_task_appears_in_list(client):
    test_client, db_path = client
    insert_guild(db_path, "guild08")
    worker_id = _create_worker(test_client, "guild08")
    headers = _auth(db_path)

    test_client.post(
        f"/guilds/guild08/workers/{worker_id}/tasks",
        json={"description": "Task one", "tool": "claude"},
        headers=headers,
    )
    test_client.post(
        f"/guilds/guild08/workers/{worker_id}/tasks",
        json={"description": "Task two", "tool": "codex"},
        headers=headers,
    )

    resp = test_client.get(f"/guilds/guild08/workers/{worker_id}/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 2
    descriptions = {t["description"] for t in tasks}
    assert descriptions == {"Task one", "Task two"}


def test_spawn_worker_env_forwards_claude_oauth_token():
    """CLAUDE_CODE_OAUTH_TOKEN must reach the spawned container so it skips setup-token."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=["owner/repo"],
        worker_name=None,
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oauth-abc"},
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oauth-abc"
    assert env["PIONEER_GUILD_ID"] == "g1"
    assert env["PIONEER_REPOS"] == "owner/repo"


def test_spawn_worker_env_does_not_forward_anthropic_api_key():
    """ANTHROPIC_API_KEY is foreman-only; workers use OAuth."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"ANTHROPIC_API_KEY": "sk-ant-api-xyz"},
    )
    assert "ANTHROPIC_API_KEY" not in env


def test_spawn_worker_env_omits_unset_keys():
    """Empty/unset auth keys must not be passed — the worker checks truthiness."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": "", "GITHUB_TOKEN": ""},
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "PIONEER_GITHUB_TOKEN" not in env


def test_spawn_worker_env_forwards_github_token_under_both_names():
    """GITHUB_TOKEN goes both as PIONEER_GITHUB_TOKEN (config loader) and GITHUB_TOKEN (gh CLI)."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=["o/r"],
        worker_name=None,
        source_env={"GITHUB_TOKEN": "ghp_xyz"},
    )
    assert env["GITHUB_TOKEN"] == "ghp_xyz"
    assert env["PIONEER_GITHUB_TOKEN"] == "ghp_xyz"


def test_spawn_worker_env_uses_worker_backend_url():
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"WORKER_BACKEND_URL": "http://custom-backend:9000"},
    )
    assert env["PIONEER_BACKEND_URL"] == "http://custom-backend:9000"


def test_spawn_worker_env_default_backend_url():
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(guild_id="g1", repos=[], worker_name=None, source_env={})
    assert env["PIONEER_BACKEND_URL"] == "http://backend:8000"


def test_spawn_worker_env_frontend_url_forwarded():
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"FRONTEND_URL": "https://pioneer-square.melloy.life"},
    )
    assert env["PIONEER_FRONTEND_URL"] == "https://pioneer-square.melloy.life"


def test_spawn_worker_env_frontend_url_absent():
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(guild_id="g1", repos=[], worker_name=None, source_env={})
    assert "PIONEER_FRONTEND_URL" not in env


def test_spawn_worker_env_frontend_url_trailing_slash_stripped():
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"FRONTEND_URL": "https://pioneer-square.melloy.life/"},
    )
    assert env["PIONEER_FRONTEND_URL"] == "https://pioneer-square.melloy.life"


def test_spawn_worker_env_db_token_beats_host_env():
    """The DB-stored token wins over the host CLAUDE_CODE_OAUTH_TOKEN."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": "stale-host-token"},
        claude_oauth_token="fresh-db-token",
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "fresh-db-token"


def test_spawn_worker_env_falls_back_to_host_env_when_db_empty():
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": "host-token"},
        claude_oauth_token=None,
    )
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "host-token"


def test_decode_claude_oauth_token_modern_format():
    """base64(json({'oauth_token': '...'})) is the format setup-token writes."""
    import base64
    import json

    from main import _decode_claude_oauth_token

    blob = base64.b64encode(json.dumps({"oauth_token": "sk-ant-oauth-real"}).encode()).decode()
    assert _decode_claude_oauth_token(blob) == "sk-ant-oauth-real"


def test_decode_claude_oauth_token_handles_empty_and_invalid():
    from main import _decode_claude_oauth_token

    assert _decode_claude_oauth_token(None) is None
    assert _decode_claude_oauth_token("") is None
    assert _decode_claude_oauth_token("not-base64!!!") is None
    # Legacy tarball blob (binary, not JSON) — silently ignored so the worker's
    # HTTP fetch path can handle it on disk instead.
    import base64

    not_json = base64.b64encode(b"\x1f\x8b\x08random tar bytes").decode()
    assert _decode_claude_oauth_token(not_json) is None


def test_decode_claude_oauth_token_missing_key():
    """JSON without oauth_token shouldn't crash."""
    import base64
    import json

    from main import _decode_claude_oauth_token

    blob = base64.b64encode(json.dumps({"other_key": "value"}).encode()).decode()
    assert _decode_claude_oauth_token(blob) is None


def test_guild_task_list(client):
    """GET /guilds/{id}/tasks lists tasks across all workers in the guild."""
    test_client, db_path = client
    insert_guild(db_path, "guild09")
    w1 = _create_worker(test_client, "guild09")
    w2 = _create_worker(test_client, "guild09")
    headers = _auth(db_path)

    test_client.post(
        f"/guilds/guild09/workers/{w1}/tasks",
        json={"description": "Alpha", "tool": "claude"},
        headers=headers,
    )
    test_client.post(
        f"/guilds/guild09/workers/{w2}/tasks",
        json={"description": "Beta", "tool": "claude"},
        headers=headers,
    )

    resp = test_client.get("/guilds/guild09/tasks", headers=headers)
    assert resp.status_code == 200
    descriptions = {t["description"] for t in resp.json()}
    assert "Alpha" in descriptions
    assert "Beta" in descriptions
