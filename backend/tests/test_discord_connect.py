"""Tests for the /connect-account slash command and POST /api/discord/connect.

Covers: (1) the pure-unit `_interaction_user` helper, (2) `_cmd_connect_account`
token minting with all DB/Discord calls mocked, and (3) the HTTP endpoint that
redeems a token (valid, expired, already-used, unknown, unauthenticated).
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import _sync_session, make_auth_token
from models import DiscordAccountLink, DiscordConnectToken

# ---------------------------------------------------------------------------
# _interaction_user (pure unit)
# ---------------------------------------------------------------------------


def test_interaction_user_from_guild_member():
    from routes.discord import _interaction_user

    interaction = {"member": {"user": {"id": "123", "username": "alice", "global_name": "Alice"}}}
    assert _interaction_user(interaction) == ("123", "Alice")


def test_interaction_user_falls_back_to_username():
    from routes.discord import _interaction_user

    interaction = {"member": {"user": {"id": "123", "username": "alice"}}}
    assert _interaction_user(interaction) == ("123", "alice")


def test_interaction_user_from_dm():
    from routes.discord import _interaction_user

    interaction = {"user": {"id": "456", "username": "bob"}}
    assert _interaction_user(interaction) == ("456", "bob")


def test_interaction_user_missing():
    from routes.discord import _interaction_user

    assert _interaction_user({}) is None
    assert _interaction_user({"member": {}}) is None


# ---------------------------------------------------------------------------
# _cmd_connect_account (DB calls mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_connect_account_mints_token_and_sends_link(monkeypatch):
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()

    sent = []

    async def fake_patch(path, payload):
        sent.append(payload)

    interaction = {
        "token": "tok-connect",
        "member": {"user": {"id": "999", "username": "carol", "global_name": "Carol"}},
    }

    with (
        patch("routes.discord.AsyncSessionLocal", return_value=mock_db),
        patch("routes.discord._discord_patch", new=fake_patch),
        patch("routes.discord.FRONTEND_URL", "https://pioneer-square.example"),
    ):
        from routes.discord import _cmd_connect_account

        await _cmd_connect_account(interaction)

    assert mock_db.add.called
    added_token = mock_db.add.call_args[0][0]
    assert added_token.discord_user_id == "999"
    assert added_token.discord_username == "Carol"
    assert added_token.used_at is None
    assert mock_db.commit.called

    assert sent
    content = sent[0]["content"]
    assert "https://pioneer-square.example/auth/discord/connect?token=" in content
    assert added_token.token in content


@pytest.mark.asyncio
async def test_cmd_connect_account_unknown_user_sends_error(monkeypatch):
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    sent = []

    async def fake_patch(path, payload):
        sent.append(payload)

    with patch("routes.discord._discord_patch", new=fake_patch):
        from routes.discord import _cmd_connect_account

        await _cmd_connect_account({"token": "tok-noone"})

    assert sent
    assert "Could not determine" in sent[0]["content"]


# ---------------------------------------------------------------------------
# POST /api/discord/connect (HTTP endpoint, real test DB)
# ---------------------------------------------------------------------------


def _insert_connect_token(
    db_url: str,
    token: str | None = None,
    discord_user_id: str = "dc-user-1",
    discord_username: str = "dc-username",
    expires_in: timedelta = timedelta(minutes=15),
    used: bool = False,
) -> str:
    token = token or str(uuid.uuid4())
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.add(
            DiscordConnectToken(
                token=token,
                discord_user_id=discord_user_id,
                discord_username=discord_username,
                expires_at=now + expires_in,
                used_at=now if used else None,
                created_at=now,
            )
        )
        session.commit()
    return token


def test_connect_requires_auth(client):
    test_client, _ = client
    resp = test_client.post("/api/discord/connect", json={"token": "whatever"})
    assert resp.status_code == 401


def test_connect_unknown_token_returns_404(client):
    test_client, db_url = client
    token = make_auth_token(db_url, user_id="gh-conn-1", username="conn1")
    headers = {"Authorization": f"Bearer {token}"}

    resp = test_client.post(
        "/api/discord/connect", json={"token": "does-not-exist"}, headers=headers
    )
    assert resp.status_code == 404


def test_connect_valid_token_links_account(client):
    test_client, db_url = client
    login_token = make_auth_token(db_url, user_id="gh-conn-2", username="conn2")
    headers = {"Authorization": f"Bearer {login_token}"}
    connect_token = _insert_connect_token(db_url, discord_user_id="dc-2", discord_username="dUser2")

    resp = test_client.post("/api/discord/connect", json={"token": connect_token}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "linked"
    assert body["discord_user_id"] == "dc-2"
    assert body["discord_username"] == "dUser2"

    with _sync_session(db_url) as session:
        link = session.query(DiscordAccountLink).filter_by(discord_user_id="dc-2").one_or_none()
        assert link is not None
        assert link.ps_user_id == "gh-conn-2"

        used_row = session.query(DiscordConnectToken).filter_by(token=connect_token).one_or_none()
        assert used_row.used_at is not None


def test_connect_already_used_token_returns_400(client):
    test_client, db_url = client
    login_token = make_auth_token(db_url, user_id="gh-conn-3", username="conn3")
    headers = {"Authorization": f"Bearer {login_token}"}
    connect_token = _insert_connect_token(db_url, discord_user_id="dc-3", used=True)

    resp = test_client.post("/api/discord/connect", json={"token": connect_token}, headers=headers)
    assert resp.status_code == 400
    assert "already been used" in resp.json()["detail"]


def test_connect_expired_token_returns_400(client):
    test_client, db_url = client
    login_token = make_auth_token(db_url, user_id="gh-conn-4", username="conn4")
    headers = {"Authorization": f"Bearer {login_token}"}
    connect_token = _insert_connect_token(
        db_url, discord_user_id="dc-4", expires_in=timedelta(minutes=-1)
    )

    resp = test_client.post("/api/discord/connect", json={"token": connect_token}, headers=headers)
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]


def test_connect_relinking_discord_user_updates_owner(client):
    """Re-running /connect-account for the same Discord user updates ps_user_id."""
    test_client, db_url = client
    first_login = make_auth_token(db_url, user_id="gh-conn-5a", username="conn5a")
    second_login = make_auth_token(db_url, user_id="gh-conn-5b", username="conn5b")

    token_1 = _insert_connect_token(db_url, discord_user_id="dc-5", discord_username="dUser5")
    resp1 = test_client.post(
        "/api/discord/connect",
        json={"token": token_1},
        headers={"Authorization": f"Bearer {first_login}"},
    )
    assert resp1.status_code == 200

    token_2 = _insert_connect_token(db_url, discord_user_id="dc-5", discord_username="dUser5")
    resp2 = test_client.post(
        "/api/discord/connect",
        json={"token": token_2},
        headers={"Authorization": f"Bearer {second_login}"},
    )
    assert resp2.status_code == 200

    with _sync_session(db_url) as session:
        link = session.query(DiscordAccountLink).filter_by(discord_user_id="dc-5").one_or_none()
        assert link is not None
        assert link.ps_user_id == "gh-conn-5b"
