"""Tests for guild REST endpoints."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import _sync_session, insert_guild, make_auth_token
from models import Guild
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col  # noqa: E402


def _insert_guild_legacy_only(db_url: str, guild_id: str, user_id: str) -> None:
    """Insert a guild with github_user_id set but NO guild_members row.

    Simulates a pre-migration guild to verify the legacy fallback is gone.
    """

    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(Guild)
            .values(slug=guild_id, created_at=now, name="Legacy Guild", github_user_id=user_id)
            .on_conflict_do_nothing()
        )
        session.commit()


def test_get_guild_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/guilds/anything")
    assert resp.status_code == 401


def test_get_guild_not_found(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    resp = test_client.get(
        "/guilds/doesnotexist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


def test_get_guild_found(client):
    test_client, db_url = client
    insert_guild(db_url, "abc123", name="My Guild")
    token = make_auth_token(db_url)
    resp = test_client.get(
        "/guilds/abc123",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "abc123"
    assert data["name"] == "My Guild"
    assert "agents" in data
    assert "messages" in data


def test_message_dict_exposes_task_id_as_camelcase():
    """History reload must carry the child-context id under `taskId` (the key
    the live WS broadcast and the frontend badge use), not the raw `task_id`
    column name row_to_dict would otherwise emit."""
    from models import Message
    from routes.guilds import _message_dict

    m = Message(
        guild_id=1,
        from_agent="foreman",
        to_agent="user",
        content="working on it",
        message_type="chat",
        task_id="t-abc",
    )
    d = _message_dict(m)
    assert d["taskId"] == "t-abc"
    assert "task_id" not in d


def test_message_dict_parent_message_has_null_task_id():
    from models import Message
    from routes.guilds import _message_dict

    m = Message(
        guild_id=1,
        from_agent="foreman",
        to_agent="user",
        content="guild status",
        message_type="chat",
    )
    d = _message_dict(m)
    assert d["taskId"] is None
    assert "task_id" not in d


def test_message_dict_merges_meta_and_keeps_task_badge():
    """A child-context tool_use row keeps both its merged meta (toolName/toolId)
    and its camelCase taskId badge after serialization."""
    import json

    from models import Message
    from routes.guilds import _message_dict

    m = Message(
        guild_id=1,
        from_agent="foreman",
        to_agent="user",
        content="▶ send_followup",
        message_type="chat",
        role="tool_use",
        task_id="t-abc",
        meta=json.dumps({"toolId": "tu-1", "toolName": "send_followup"}),
    )
    d = _message_dict(m)
    assert d["taskId"] == "t-abc"
    assert d["toolName"] == "send_followup"
    assert d["toolId"] == "tu-1"


def test_get_guild_forbidden_for_non_member(client):
    test_client, db_url = client
    insert_guild(db_url, "private01", name="Private")
    other_token = make_auth_token(db_url, user_id="other-user", username="outsider")
    resp = test_client.get(
        "/guilds/private01",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


def test_create_guild_requires_auth(client):
    test_client, _ = client
    resp = test_client.post("/guilds", json={})
    assert resp.status_code == 401


def test_create_guild_returns_id(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/guilds",
        json={"name": "Test Guild"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data
    assert data["name"] == "Test Guild"
    assert len(data["id"]) >= 2


def test_create_guild_default_name(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    resp = test_client.post(
        "/guilds",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"].startswith("Guild ")


def test_list_guilds_requires_auth(client):
    test_client, _ = client
    resp = test_client.get("/guilds")
    assert resp.status_code == 401


def test_list_guilds_returns_created_guild(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    test_client.post("/guilds", json={"name": "Guild A"}, headers=headers)
    test_client.post("/guilds", json={"name": "Guild B"}, headers=headers)

    resp = test_client.get("/guilds", headers=headers)
    assert resp.status_code == 200
    names = [g["name"] for g in resp.json()]
    assert "Guild A" in names
    assert "Guild B" in names


def test_update_guild_name(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = test_client.post("/guilds", json={"name": "Old Name"}, headers=headers)
    guild_id = create_resp.json()["id"]

    patch_resp = test_client.patch(
        f"/guilds/{guild_id}",
        json={"name": "New Name"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "New Name"

    get_resp = test_client.get(f"/guilds/{guild_id}", headers=headers)
    assert get_resp.json()["name"] == "New Name"


def test_update_guild_not_found(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    resp = test_client.patch(
        "/guilds/nosuchguild",
        json={"name": "X"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (403, 404)


def test_update_guild_primary_repo(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = test_client.post("/guilds", json={"name": "Repo Guild"}, headers=headers)
    guild_id = create_resp.json()["id"]

    patch_resp = test_client.patch(
        f"/guilds/{guild_id}",
        json={"primary_repo": "owner/myrepo"},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["primary_repo"] == "owner/myrepo"

    get_resp = test_client.get(f"/guilds/{guild_id}", headers=headers)
    assert get_resp.json()["primary_repo"] == "owner/myrepo"


def test_update_guild_clear_primary_repo(client):
    test_client, db_url = client
    token = make_auth_token(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = test_client.post("/guilds", json={"name": "Clear Repo Guild"}, headers=headers)
    guild_id = create_resp.json()["id"]

    test_client.patch(f"/guilds/{guild_id}", json={"primary_repo": "owner/repo"}, headers=headers)

    # Clear by sending null
    patch_resp = test_client.patch(
        f"/guilds/{guild_id}",
        json={"primary_repo": None},
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["primary_repo"] is None


def test_guild_partial_unique_index(client):
    """slug must be unique among active guilds but reusable after soft-delete."""
    from sqlalchemy.exc import IntegrityError

    test_client, db_url = client  # noqa: F841 — db_url used directly

    shared_id = "reuse-me-001"
    now = datetime.now(UTC)

    with _sync_session(db_url) as session:
        # Insert the first active guild.
        session.add(Guild(slug=shared_id, created_at=now, name="First"))
        session.commit()

    # A second guild with the same slug (both active) must violate the
    # partial unique index.
    with pytest.raises(Exception) as exc_info:
        with _sync_session(db_url) as session:
            session.add(Guild(slug=shared_id, created_at=now, name="Duplicate"))
            session.commit()
    # Verify it is an integrity/uniqueness violation
    assert exc_info.type.__name__ in ("UniqueViolation", "IntegrityError") or (
        "unique" in str(exc_info.value).lower() or "duplicate" in str(exc_info.value).lower()
    )

    # Soft-delete the first guild.
    with _sync_session(db_url) as session:
        session.execute(
            update(Guild)
            .where(col(Guild.slug) == shared_id, col(Guild.deleted_at).is_(None))
            .values(deleted_at=now)
        )
        session.commit()

    # After soft-delete the same guild_id can be inserted again.
    with _sync_session(db_url) as session:
        session.add(Guild(slug=shared_id, created_at=now, name="Third"))
        session.commit()
        count = session.scalar(
            select(func.count()).select_from(Guild).where(col(Guild.slug) == shared_id)
        )
    assert count == 2, "should have one deleted and one active row for the same slug"


def test_primary_repo_in_foreman_prompt():

    from foreman.prompt import build_system_prompt

    # Primary repo is included when set
    prompt = build_system_prompt("[]", "[]", primary_repo="jmelloy/pioneer-square")
    assert "jmelloy/pioneer-square" in prompt
    assert "primary repository" in prompt.lower()

    # Primary repo line is absent when not set
    prompt_no_repo = build_system_prompt("[]", "[]")
    assert "primary repository" not in prompt_no_repo.lower()

    # Primary repo line is absent when explicitly None
    prompt_none = build_system_prompt("[]", "[]", primary_repo=None)
    assert "primary repository" not in prompt_none.lower()

    # extra_context still works alongside primary_repo
    prompt_both = build_system_prompt("[]", "[]", extra_context="some context", primary_repo="o/r")
    assert "o/r" in prompt_both
    assert "some context" in prompt_both


# ---------------------------------------------------------------------------
# guild_members migration — legacy github_user_id paths must be gone
# ---------------------------------------------------------------------------


def test_list_guilds_excludes_legacy_owner(client):
    """Guilds where guilds.github_user_id matches but no guild_members row exists
    must NOT appear in the list; the legacy OR-clause has been removed."""
    test_client, db_url = client
    token = make_auth_token(db_url)  # user_id="gh-user-test"
    _insert_guild_legacy_only(db_url, "g-legacy", "gh-user-test")
    resp = test_client.get("/guilds", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    guild_ids = [g["id"] for g in resp.json()]
    assert "g-legacy" not in guild_ids


def test_get_guild_rejects_legacy_owner(client):
    """A user whose id is only in guilds.github_user_id (no guild_members row)
    must get 403, not 200 — the ensure_membership legacy fallback is gone."""
    test_client, db_url = client
    token = make_auth_token(db_url)  # user_id="gh-user-test"
    _insert_guild_legacy_only(db_url, "g-leg2", "gh-user-test")
    resp = test_client.get("/guilds/g-leg2", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# foreman-config env vars — stored and returned in clear text (no masking)
# ---------------------------------------------------------------------------


def test_foreman_config_env_vars_round_trip_in_clear(client):
    """PATCHed env var values (including secrets) come back verbatim on GET.

    Masking was intentionally removed so owners can verify and copy/paste values.
    """
    test_client, db_url = client
    token = make_auth_token(db_url)  # owner of g-fconf
    insert_guild(db_url, "g-fconf")
    headers = {"Authorization": f"Bearer {token}"}

    patch = test_client.patch(
        "/api/guilds/g-fconf/foreman-config",
        headers=headers,
        json={
            "provider": "bedrock",
            "model": "arn:aws:bedrock:eu-west-1:111111111111:inference-profile/us.anthropic.claude-sonnet-4-6",
            "env_vars": [
                {"key": "AWS_DEFAULT_REGION", "value": "eu-west-1"},
                {"key": "AWS_ACCESS_KEY_ID", "value": "AKIASECRET"},
                {"key": "AWS_SECRET_ACCESS_KEY", "value": "topsecret"},
            ],
        },
    )
    assert patch.status_code == 200

    got = test_client.get("/api/guilds/g-fconf/foreman-config", headers=headers)
    assert got.status_code == 200
    env = {e["key"]: e["value"] for e in got.json()["env_vars"]}
    assert env == {
        "AWS_DEFAULT_REGION": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "AKIASECRET",
        "AWS_SECRET_ACCESS_KEY": "topsecret",  # secret returned in clear, not masked
    }


def test_foreman_config_bedrock_without_model_rejected(client, monkeypatch):
    """Regression for #817: saving provider=bedrock with no model configured must be
    rejected up front — Bedrock inference profiles are AWS-account-scoped, so silently
    accepting this would only fail later, opaquely, against a placeholder ARN."""
    monkeypatch.delenv("FOREMAN_BEDROCK_MODEL", raising=False)
    test_client, db_url = client
    token = make_auth_token(db_url)
    insert_guild(db_url, "g-fconf2")
    headers = {"Authorization": f"Bearer {token}"}

    patch = test_client.patch(
        "/api/guilds/g-fconf2/foreman-config",
        headers=headers,
        json={"provider": "bedrock"},
    )
    assert patch.status_code == 400

    got = test_client.get("/api/guilds/g-fconf2/foreman-config", headers=headers)
    assert got.json() == {}


def test_foreman_config_bedrock_with_model_accepted(client, monkeypatch):
    """A model supplied alongside provider=bedrock passes validation."""
    monkeypatch.delenv("FOREMAN_BEDROCK_MODEL", raising=False)
    test_client, db_url = client
    token = make_auth_token(db_url)
    insert_guild(db_url, "g-fconf3")
    headers = {"Authorization": f"Bearer {token}"}

    patch = test_client.patch(
        "/api/guilds/g-fconf3/foreman-config",
        headers=headers,
        json={
            "provider": "bedrock",
            "model": "arn:aws:bedrock:us-east-1:111111111111:inference-profile/us.anthropic.claude-sonnet-4-6",
        },
    )
    assert patch.status_code == 200
