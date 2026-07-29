"""Tests for worker and task REST endpoints."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from datetime import UTC, datetime, timedelta

from helpers import _sync_session, insert_guild, insert_member, make_auth_token
from models import Agent, ClaudeCredentials, Guild, GuildSpawnDefaults, User, Worker
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col  # noqa: E402


def _auth(db_url: str) -> dict:
    """Helper: return Authorization header for the default test user."""
    return {"Authorization": f"Bearer {make_auth_token(db_url)}"}


def _set_guild_env(test_client, db_url: str, slug: str, env_vars: list[dict]) -> None:
    """Set a guild's env vars via the real PATCH endpoint.

    Routes through PATCH /guilds/{slug}, which merges/dedups and syncs the
    forwarded worker-slice into the spawn_settings baseline (the source the spawn
    path resolves from). ``env_vars`` items are {key, value, forward} dicts.
    """
    resp = test_client.patch(
        f"/api/guilds/{slug}/foreman-config", headers=_auth(db_url), json={"env_vars": env_vars}
    )
    assert resp.status_code == 200, resp.text


def _workers_for_guild(db_url: str, guild_id: str) -> list[Worker]:
    """Query workers for *guild_id* directly (no list-workers REST endpoint exists)."""
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == guild_id))
        return list(session.scalars(select(Worker).where(col(Worker.guild_id) == guild_pk)).all())


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def test_list_workers_empty(client):
    test_client, db_url = client
    insert_guild(db_url, "guild01")
    assert _workers_for_guild(db_url, "guild01") == []


def test_create_worker_returns_id(client):
    test_client, db_url = client
    insert_guild(db_url, "guild02")
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
    test_client, db_url = client
    insert_guild(db_url, "guild03")
    test_client.post("/guilds/guild03/workers", json={"repos": ["org/backend"]})
    workers = _workers_for_guild(db_url, "guild03")
    assert len(workers) == 1
    assert isinstance(workers[0].guild_id, int)


def test_create_worker_does_not_insert_agent_row(client):
    """Worker registration must not create a phantom agent row (issue #264).

    Agent rows are created only when the worker process sends a ``join``
    message over WebSocket, not during REST-based registration.
    """

    test_client, db_url = client
    insert_guild(db_url, "guild03b")
    resp = test_client.post("/guilds/guild03b/workers", json={"repos": []})
    assert resp.status_code == 200
    worker_id = resp.json()["id"]

    with _sync_session(db_url) as session:
        row = session.scalar(select(col(Agent.id)).where(col(Agent.id) == worker_id))
    assert row is None, f"create_worker must not insert an agent row, but found: {row}"


def test_create_multiple_workers(client):
    test_client, db_url = client
    insert_guild(db_url, "guild04")
    test_client.post("/guilds/guild04/workers", json={"repos": []})
    test_client.post("/guilds/guild04/workers", json={"repos": ["x/y"]})
    assert len(_workers_for_guild(db_url, "guild04")) == 2


def test_create_worker_attributes_to_user(client):
    """Worker registration with `user` resolves to a users.id and stores it on workers.user_id."""

    test_client, db_url = client
    insert_guild(db_url, "guildusr")
    insert_member(db_url, "guildusr", "user-bob", role="member")
    # Bob's users row was created by insert_member; reference by login should also work.
    with _sync_session(db_url) as session:
        session.execute(update(User).where(col(User.id) == "user-bob").values(github_login="bobby"))
        session.commit()
    resp = test_client.post(
        "/guilds/guildusr/workers",
        json={"repos": [], "user": "bobby"},
    )
    assert resp.status_code == 200
    worker_id = resp.json()["id"]
    with _sync_session(db_url) as session:
        user_id = session.scalar(select(col(Worker.user_id)).where(col(Worker.id) == worker_id))
    assert user_id == "user-bob"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _create_worker(test_client, guild_id: str) -> str:
    resp = test_client.post(f"/guilds/{guild_id}/workers", json={"repos": []})
    return resp.json()["id"]


def test_list_tasks_empty(client):
    test_client, db_url = client
    insert_guild(db_url, "guild05")
    worker_id = _create_worker(test_client, "guild05")
    resp = test_client.get(f"/guilds/guild05/workers/{worker_id}/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_assign_task_unknown_worker(client):
    test_client, db_url = client
    insert_guild(db_url, "guild06")
    resp = test_client.post(
        "/guilds/guild06/workers/w-nosuch/tasks",
        json={"description": "do something", "tool": "claude"},
        headers=_auth(db_url),
    )
    assert resp.status_code == 404


def test_assign_task_returns_pending(client):
    test_client, db_url = client
    insert_guild(db_url, "guild07")
    worker_id = _create_worker(test_client, "guild07")

    resp = test_client.post(
        f"/guilds/guild07/workers/{worker_id}/tasks",
        json={"description": "Write a hello-world script", "tool": "claude"},
        headers=_auth(db_url),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "pending"
    assert data["worker_id"] == worker_id
    assert data["id"].startswith("t-")


def test_assign_task_appears_in_list(client):
    test_client, db_url = client
    insert_guild(db_url, "guild08")
    worker_id = _create_worker(test_client, "guild08")
    headers = _auth(db_url)

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


# ---------------------------------------------------------------------------
# Worker shutdown
# ---------------------------------------------------------------------------


def test_shutdown_worker_signals_and_disables(client, monkeypatch):
    """The shutdown endpoint broadcasts a worker-shutdown signal and disables the worker."""
    test_client, db_url = client
    insert_guild(db_url, "guild-shutdown")
    worker_id = _create_worker(test_client, "guild-shutdown")

    broadcasts: list = []

    async def fake_broadcast(guild_id, msg, *a, **kw):
        broadcasts.append((guild_id, msg))

    # Don't schedule the real force-kill poll loop during the test — just close
    # the coroutine so it isn't reported as never-awaited.
    def fake_spawn(coro, **kw):
        coro.close()
        return None

    monkeypatch.setattr("routes.workers.broadcast_msg", fake_broadcast)
    monkeypatch.setattr("routes.workers.spawn", fake_spawn)

    resp = test_client.post(
        f"/guilds/guild-shutdown/workers/{worker_id}/shutdown",
        headers=_auth(db_url),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "shutdown-signalled"

    # A graceful worker-shutdown signal was broadcast to this worker.
    assert any(
        getattr(msg, "workerId", None) == worker_id
        and getattr(msg, "type", None) == "worker-shutdown"
        for _, msg in broadcasts
    )

    # The worker is now disabled so it isn't respawned on the next backend restart.
    with _sync_session(db_url) as session:
        disabled = session.execute(
            select(col(Worker.disabled)).where(col(Worker.id) == worker_id)
        ).scalar_one()
    assert disabled is True


def test_shutdown_unknown_worker_returns_404(client):
    test_client, db_url = client
    insert_guild(db_url, "guild-shutdown-404")
    resp = test_client.post(
        "/guilds/guild-shutdown-404/workers/w-nope/shutdown",
        headers=_auth(db_url),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Guild spawn defaults
# ---------------------------------------------------------------------------


def test_get_spawn_defaults_empty(client):
    """A guild with no recorded spawn returns empty defaults, not 404."""
    test_client, db_url = client
    insert_guild(db_url, "guild-sd1")
    resp = test_client.get("/guilds/guild-sd1/spawn-defaults", headers=_auth(db_url))
    assert resp.status_code == 200
    assert resp.json() == {"repos": [], "tools": [], "agent_count": None}


def test_put_spawn_defaults_roundtrips(client):
    test_client, db_url = client
    insert_guild(db_url, "guild-sd2")
    resp = test_client.put(
        "/guilds/guild-sd2/spawn-defaults",
        headers=_auth(db_url),
        json={"repos": ["a/b", "a/c"], "tools": ["claude"], "agent_count": 3},
    )
    assert resp.status_code == 200
    assert resp.json() == {"repos": ["a/b", "a/c"], "tools": ["claude"], "agent_count": 3}
    # A subsequent GET reflects the saved baseline.
    got = test_client.get("/guilds/guild-sd2/spawn-defaults", headers=_auth(db_url))
    assert got.json() == {"repos": ["a/b", "a/c"], "tools": ["claude"], "agent_count": 3}


def test_put_spawn_defaults_full_replace_clears_fields(client):
    """PUT has full-replace semantics: an empty body wipes the previous baseline."""
    test_client, db_url = client
    insert_guild(db_url, "guild-sd3")
    test_client.put(
        "/guilds/guild-sd3/spawn-defaults",
        headers=_auth(db_url),
        json={"repos": ["a/b"], "tools": ["codex"], "agent_count": 5},
    )
    resp = test_client.put(
        "/guilds/guild-sd3/spawn-defaults",
        headers=_auth(db_url),
        json={},
    )
    assert resp.status_code == 200
    assert resp.json() == {"repos": [], "tools": [], "agent_count": None}


def test_put_spawn_defaults_rejects_bad_agent_count(client):
    test_client, db_url = client
    insert_guild(db_url, "guild-sd4")
    resp = test_client.put(
        "/guilds/guild-sd4/spawn-defaults",
        headers=_auth(db_url),
        json={"repos": [], "tools": [], "agent_count": 99},
    )
    assert resp.status_code == 422


def test_put_spawn_defaults_requires_owner(client):
    """Non-owner members can read defaults but not set them."""
    test_client, db_url = client
    insert_guild(db_url, "guild-sd5")
    insert_member(db_url, "guild-sd5", "gh-member", role="member")
    member_auth = {
        "Authorization": f"Bearer {make_auth_token(db_url, user_id='gh-member', username='member1')}"
    }
    # Member can GET.
    got = test_client.get("/guilds/guild-sd5/spawn-defaults", headers=member_auth)
    assert got.status_code == 200
    # But cannot PUT.
    resp = test_client.put(
        "/guilds/guild-sd5/spawn-defaults",
        headers=member_auth,
        json={"repos": ["a/b"], "tools": [], "agent_count": 2},
    )
    assert resp.status_code == 403


def test_get_spawn_credentials_empty(client):
    """A guild with no foreman env vars and no Claude credentials reports both as unset."""
    test_client, db_url = client
    insert_guild(db_url, "guild-cred1")
    resp = test_client.get("/guilds/guild-cred1/spawn-credentials", headers=_auth(db_url))
    assert resp.status_code == 200
    assert resp.json() == {
        "guild_env_vars": [],
        "claude_credentials": {"saved": False, "updated_at": None},
    }


def test_get_spawn_credentials_masks_guild_env_var_values(client):
    """Guild foreman env var values are never returned in the clear from this endpoint."""
    test_client, db_url = client
    insert_guild(db_url, "guild-cred2")
    _set_guild_env(
        test_client,
        db_url,
        "guild-cred2",
        [{"key": "ANTHROPIC_API_KEY", "value": "sk-ant-supersecretvalue", "forward": True}],
    )
    resp = test_client.get("/guilds/guild-cred2/spawn-credentials", headers=_auth(db_url))
    assert resp.status_code == 200
    body = resp.json()
    assert body["guild_env_vars"] == [{"key": "ANTHROPIC_API_KEY", "masked_value": "sk…alue"}]
    assert "sk-ant-supersecretvalue" not in resp.text


def test_get_spawn_credentials_reports_claude_creds_saved(client):
    test_client, db_url = client
    insert_guild(db_url, "guild-cred3")
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == "guild-cred3"))
        session.execute(
            pg_insert(ClaudeCredentials).values(
                guild_id=guild_pk,
                credentials_blob="ignored-for-this-test",
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()
    resp = test_client.get("/guilds/guild-cred3/spawn-credentials", headers=_auth(db_url))
    assert resp.status_code == 200
    body = resp.json()
    assert body["claude_credentials"]["saved"] is True
    assert body["claude_credentials"]["updated_at"] is not None
    # The raw blob is never part of the response.
    assert "ignored-for-this-test" not in resp.text


def test_spawn_worker_excludes_selected_guild_env_keys(client, monkeypatch):
    """exclude_env_keys drops those guild-level credentials from this spawn only."""
    test_client, db_url = client
    insert_guild(db_url, "guild-cred4")
    _set_guild_env(
        test_client,
        db_url,
        "guild-cred4",
        [
            {"key": "ANTHROPIC_API_KEY", "value": "keep-me", "forward": True},
            {"key": "OPENAI_API_KEY", "value": "drop-me", "forward": True},
        ],
    )

    import worker_runtime

    captured_env: dict = {}

    async def fake_check_runtime_available():
        return None

    async def fake_start_worker_container(*, env, guild_id):
        captured_env.update(env)
        return worker_runtime.SpawnedWorker(
            handle="fake-handle", short_id="fakehandle12", runtime="docker"
        )

    monkeypatch.setattr(worker_runtime, "check_runtime_available", fake_check_runtime_available)
    monkeypatch.setattr(worker_runtime, "start_worker_container", fake_start_worker_container)

    resp = test_client.post(
        "/guilds/guild-cred4/spawn-worker",
        headers=_auth(db_url),
        json={"repos": ["a/b"], "exclude_env_keys": ["OPENAI_API_KEY"]},
    )
    assert resp.status_code == 200
    assert captured_env.get("ANTHROPIC_API_KEY") == "keep-me"
    assert "OPENAI_API_KEY" not in captured_env
    # The guild's stored credential is untouched — only this spawn skipped it.
    stored = test_client.get("/guilds/guild-cred4/spawn-credentials", headers=_auth(db_url))
    stored_keys = {e["key"] for e in stored.json()["guild_env_vars"]}
    assert stored_keys == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}


def test_spawn_worker_only_forwards_flagged_guild_env_vars(client, monkeypatch):
    """A guild env var without forward=True stays with the foreman and must not
    reach the spawned worker; a forwarded one does."""
    test_client, db_url = client
    insert_guild(db_url, "guild-fwd")
    _set_guild_env(
        test_client,
        db_url,
        "guild-fwd",
        [
            {"key": "SHARED_TOKEN", "value": "yes", "forward": True},
            {"key": "FOREMAN_ONLY", "value": "no", "forward": False},  # not forwarded
        ],
    )

    import worker_runtime

    captured_env: dict = {}

    async def fake_check_runtime_available():
        return None

    async def fake_start_worker_container(*, env, guild_id):
        captured_env.update(env)
        return worker_runtime.SpawnedWorker(
            handle="fake-handle", short_id="fakehandle12", runtime="docker"
        )

    monkeypatch.setattr(worker_runtime, "check_runtime_available", fake_check_runtime_available)
    monkeypatch.setattr(worker_runtime, "start_worker_container", fake_start_worker_container)

    resp = test_client.post(
        "/guilds/guild-fwd/spawn-worker",
        headers=_auth(db_url),
        json={"repos": ["a/b"]},
    )
    assert resp.status_code == 200
    assert captured_env.get("SHARED_TOKEN") == "yes"
    assert "FOREMAN_ONLY" not in captured_env


def test_spawn_worker_empty_user_value_does_not_clobber_guild_credential(client, monkeypatch):
    """An empty spawn-time value for a key that also exists as a guild credential
    must not blank out the credential's real value (the value wins)."""
    test_client, db_url = client
    insert_guild(db_url, "guild-cred-clob")
    _set_guild_env(
        test_client,
        db_url,
        "guild-cred-clob",
        [{"key": "AWS_BEARER_TOKEN_BEDROCK", "value": "real-token", "forward": True}],
    )

    import worker_runtime

    captured_env: dict = {}

    async def fake_check_runtime_available():
        return None

    async def fake_start_worker_container(*, env, guild_id):
        captured_env.update(env)
        return worker_runtime.SpawnedWorker(
            handle="fake-handle", short_id="fakehandle12", runtime="docker"
        )

    monkeypatch.setattr(worker_runtime, "check_runtime_available", fake_check_runtime_available)
    monkeypatch.setattr(worker_runtime, "start_worker_container", fake_start_worker_container)

    resp = test_client.post(
        "/guilds/guild-cred-clob/spawn-worker",
        headers=_auth(db_url),
        json={"repos": ["a/b"], "env_vars": {"AWS_BEARER_TOKEN_BEDROCK": ""}},
    )
    assert resp.status_code == 200
    assert captured_env.get("AWS_BEARER_TOKEN_BEDROCK") == "real-token"


def test_spawn_worker_guild_duplicate_key_prefers_nonempty(client, monkeypatch):
    """Submitting the same key twice (one real value, one blank) must collapse to
    the real value on write, so the container boots with the real value — not
    whichever entry came last."""
    test_client, db_url = client
    insert_guild(db_url, "guild-cred-dup")
    _set_guild_env(
        test_client,
        db_url,
        "guild-cred-dup",
        [
            {"key": "AWS_BEARER_TOKEN_BEDROCK", "value": "real-token", "forward": True},
            {"key": "AWS_BEARER_TOKEN_BEDROCK", "value": "", "forward": True},
        ],
    )

    import worker_runtime

    captured_env: dict = {}

    async def fake_check_runtime_available():
        return None

    async def fake_start_worker_container(*, env, guild_id):
        captured_env.update(env)
        return worker_runtime.SpawnedWorker(
            handle="fake-handle", short_id="fakehandle12", runtime="docker"
        )

    monkeypatch.setattr(worker_runtime, "check_runtime_available", fake_check_runtime_available)
    monkeypatch.setattr(worker_runtime, "start_worker_container", fake_start_worker_container)

    resp = test_client.post(
        "/guilds/guild-cred-dup/spawn-worker",
        headers=_auth(db_url),
        json={"repos": ["a/b"]},
    )
    assert resp.status_code == 200
    assert captured_env.get("AWS_BEARER_TOKEN_BEDROCK") == "real-token"


def test_spawn_worker_rejects_invalid_exclude_env_key(client):
    test_client, db_url = client
    insert_guild(db_url, "guild-cred5")
    resp = test_client.post(
        "/guilds/guild-cred5/spawn-worker",
        headers=_auth(db_url),
        json={"repos": ["a/b"], "exclude_env_keys": ["not a valid key"]},
    )
    assert resp.status_code == 422


def test_spawn_worker_rejects_when_guild_in_cooldown(client, monkeypatch):
    """A guild that spawned a worker within the cooldown window gets 429, no container started."""
    test_client, db_url = client
    insert_guild(db_url, "guild-cooldown")
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(select(col(Guild.id)).where(col(Guild.slug) == "guild-cooldown"))
        session.add(
            GuildSpawnDefaults(
                guild_id=guild_pk,
                repos="[]",
                tools="[]",
                agent_count=None,
                updated_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        session.commit()

    import worker_runtime

    started = {"called": False}

    async def fake_check_runtime_available():
        return None

    async def fake_start_worker_container(*, env, guild_id):
        started["called"] = True
        return worker_runtime.SpawnedWorker(
            handle="fake-handle", short_id="fakehandle12", runtime="docker"
        )

    monkeypatch.setattr(worker_runtime, "check_runtime_available", fake_check_runtime_available)
    monkeypatch.setattr(worker_runtime, "start_worker_container", fake_start_worker_container)

    resp = test_client.post(
        "/guilds/guild-cooldown/spawn-worker",
        headers=_auth(db_url),
        json={"repos": ["a/b"]},
    )

    assert resp.status_code == 429
    assert "cooldown" in resp.json()["detail"].lower()
    assert started["called"] is False


def test_spawn_worker_allowed_after_cooldown_expires(client, monkeypatch):
    """A guild whose last spawn is older than the cooldown window may spawn again."""
    test_client, db_url = client
    insert_guild(db_url, "guild-cooldown-ok")
    with _sync_session(db_url) as session:
        guild_pk = session.scalar(
            select(col(Guild.id)).where(col(Guild.slug) == "guild-cooldown-ok")
        )
        session.add(
            GuildSpawnDefaults(
                guild_id=guild_pk,
                repos="[]",
                tools="[]",
                agent_count=None,
                updated_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        session.commit()

    import worker_runtime

    async def fake_check_runtime_available():
        return None

    async def fake_start_worker_container(*, env, guild_id):
        return worker_runtime.SpawnedWorker(
            handle="fake-handle", short_id="fakehandle12", runtime="docker"
        )

    monkeypatch.setattr(worker_runtime, "check_runtime_available", fake_check_runtime_available)
    monkeypatch.setattr(worker_runtime, "start_worker_container", fake_start_worker_container)

    resp = test_client.post(
        "/guilds/guild-cooldown-ok/spawn-worker",
        headers=_auth(db_url),
        json={"repos": ["a/b"]},
    )
    assert resp.status_code == 200


def test_spawn_worker_env_does_not_inject_claude_oauth_token():
    """Claude credentials are per-guild: they come from the guild's foreman
    env_vars, so a backend-wide CLAUDE_CODE_OAUTH_TOKEN is NOT injected at spawn.

    Injecting a backend-wide token would shadow the per-guild credential a
    worker inherits from its guild env vars.
    """
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=["owner/repo"],
        worker_name=None,
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oauth-abc"},
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
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


def test_spawn_worker_env_never_carries_credentials():
    """Worker credentials are fetched at runtime, never injected — regardless of
    what the backend happens to hold in its own environment."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oauth",
            "GITHUB_TOKEN": "ghp_xyz",
            "OPENAI_API_KEY": "sk-openai",
            "ANTHROPIC_API_KEY": "sk-ant-api",
        },
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "PIONEER_GITHUB_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_spawn_worker_env_does_not_inject_github_token():
    """GitHub credentials are per-guild: the worker fetches them from the backend
    (/auth/github/token) on startup, so they are NOT injected at spawn."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=["o/r"],
        worker_name=None,
        source_env={"GITHUB_TOKEN": "ghp_xyz"},
    )
    assert "GITHUB_TOKEN" not in env
    assert "PIONEER_GITHUB_TOKEN" not in env


def test_spawn_worker_env_does_not_inject_provider_keys_from_backend_env():
    """Provider API keys (OpenAI, etc.) are per-guild: the worker fetches them via
    /guilds/{id}/foreman/env-vars, so a backend-wide value is NOT injected."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"OPENAI_API_KEY": "sk-openai-deploy"},
    )
    assert "OPENAI_API_KEY" not in env


def test_spawn_worker_env_passes_user_supplied_provider_key():
    """A caller-supplied (guild/spawn) key rides through extra_env untouched — the
    per-spawn override channel, distinct from the backend's own environment."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"OPENAI_API_KEY": "sk-openai-deploy"},
        extra_env={"OPENAI_API_KEY": "sk-openai-user"},
    )
    assert env["OPENAI_API_KEY"] == "sk-openai-user"


def test_spawn_worker_env_passes_arbitrary_user_env_vars():
    """User-supplied extra_env keys of any name reach the spawned worker verbatim."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={},
        extra_env={"MY_CUSTOM_TOKEN": "abc123", "SOME_API_KEY": "xyz789"},
    )
    assert env["MY_CUSTOM_TOKEN"] == "abc123"
    assert env["SOME_API_KEY"] == "xyz789"
    # Pioneer's own wiring still wins over user-supplied collisions.
    assert env["PIONEER_GUILD_ID"] == "g1"


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


def test_spawn_worker_env_forwards_debug_token():
    """DEBUG_TOKEN lets the worker's debug-query skill call /debug/query."""
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(
        guild_id="g1",
        repos=[],
        worker_name=None,
        source_env={"DEBUG_TOKEN": "operator-secret"},
    )
    assert env["DEBUG_TOKEN"] == "operator-secret"


def test_spawn_worker_env_omits_debug_token_when_unset():
    from main import _build_spawn_worker_env

    env = _build_spawn_worker_env(guild_id="g1", repos=[], worker_name=None, source_env={})
    assert "DEBUG_TOKEN" not in env


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
    test_client, db_url = client
    insert_guild(db_url, "guild09")
    w1 = _create_worker(test_client, "guild09")
    w2 = _create_worker(test_client, "guild09")
    headers = _auth(db_url)

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
