"""Tests for POST /api/guilds/{guild_id}/conversations/{conversation_id}/messages (#1297).

Covers authorization (guild membership + conversation ownership), Message
persistence, the WebSocket broadcast, and the Foreman trigger this endpoint
dispatches — the "user comments inside a conversation are treated as
followups" pattern from Epic #1271.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from foreman import triggers  # noqa: E402
from helpers import (  # noqa: E402
    _sync_session,
    insert_conversation,
    insert_guild,
    insert_member,
    make_auth_token,
)
from models import Message  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlmodel import col  # noqa: E402


def _url(guild_id: str, conversation_id: int) -> str:
    return f"/api/guilds/{guild_id}/conversations/{conversation_id}/messages"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_requires_auth(client):
    test_client, db_url = client
    insert_guild(db_url, "g-cm-auth")
    conv_id = insert_conversation(db_url, "g-cm-auth", user_id="gh-user-test")

    resp = test_client.post(_url("g-cm-auth", conv_id), json={"content": "hi"})

    assert resp.status_code == 401


def test_guild_not_found(client):
    test_client, db_url = client
    token = make_auth_token(db_url)

    resp = test_client.post(
        _url("doesnotexist", 1),
        json={"content": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404


def test_requires_guild_membership(client):
    test_client, db_url = client
    insert_guild(db_url, "g-cm-member", owner_user_id="owner-x")
    conv_id = insert_conversation(db_url, "g-cm-member", user_id="owner-x")
    token = make_auth_token(db_url, user_id="not-a-member", username="outsider")

    resp = test_client.post(
        _url("g-cm-member", conv_id),
        json={"content": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403


def test_conversation_not_found(client):
    test_client, db_url = client
    insert_guild(db_url, "g-cm-missing")
    token = make_auth_token(db_url)

    resp = test_client.post(
        _url("g-cm-missing", 999999),
        json={"content": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404


def test_rejects_non_owner(client):
    """A guild member who does not own the conversation cannot post into it."""
    test_client, db_url = client
    insert_guild(db_url, "g-cm-owner", owner_user_id="owner-a")
    insert_member(db_url, "g-cm-owner", "user-b", role="member")
    conv_id = insert_conversation(db_url, "g-cm-owner", user_id="owner-a")
    token_b = make_auth_token(db_url, user_id="user-b", username="userb")

    with patch.object(triggers, "trigger_foreman", new=AsyncMock()):
        resp = test_client.post(
            _url("g-cm-owner", conv_id),
            json={"content": "hi"},
            headers={"Authorization": f"Bearer {token_b}"},
        )

    assert resp.status_code == 403


def test_allows_posting_to_unowned_conversation(client):
    """A Conversation with no user_id (system-owned) has no single owner to enforce."""
    test_client, db_url = client
    guild_id = "g-cm-unowned"
    insert_guild(db_url, guild_id, owner_user_id="owner-c")
    insert_member(db_url, guild_id, "user-d", role="member")
    conv_id = insert_conversation(db_url, guild_id, user_id=None)
    token = make_auth_token(db_url, user_id="user-d", username="userd")

    with patch.object(triggers, "trigger_foreman", new=AsyncMock()):
        resp = test_client.post(
            _url(guild_id, conv_id),
            json={"content": "hi"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200


def test_rejects_empty_content(client):
    test_client, db_url = client
    insert_guild(db_url, "g-cm-empty")
    conv_id = insert_conversation(db_url, "g-cm-empty", user_id="gh-user-test")
    token = make_auth_token(db_url)

    resp = test_client.post(
        _url("g-cm-empty", conv_id),
        json={"content": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Persistence + Foreman trigger
# ---------------------------------------------------------------------------


def test_persists_message_and_triggers_foreman(client):
    test_client, db_url = client
    guild_id = "g-cm-ok"
    insert_guild(db_url, guild_id)
    conv_id = insert_conversation(db_url, guild_id, user_id="gh-user-test")
    token = make_auth_token(db_url)

    triggered: list[tuple[str, str, str, dict]] = []

    async def fake_trigger(guild_id_arg, event, message, **kwargs):
        triggered.append((guild_id_arg, event, message, kwargs))

    with patch.object(triggers, "trigger_foreman", new=fake_trigger):
        resp = test_client.post(
            _url(guild_id, conv_id),
            json={"content": "please continue the task"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["conversationId"] == conv_id
    assert body["content"] == "please continue the task"
    assert body["id"] is not None

    with _sync_session(db_url) as session:
        row = session.execute(
            select(Message).where(col(Message.conversation_id) == conv_id)
        ).scalar_one()
    assert row.from_agent == "user"
    assert row.to_agent == "foreman"
    assert row.content == "please continue the task"
    assert row.user_id == "gh-user-test"
    assert row.source == "api"
    assert row.message_type == "chat"

    assert len(triggered) == 1
    guild_id_arg, event, message, kwargs = triggered[0]
    assert guild_id_arg == guild_id
    assert event == "conversation-message"
    assert kwargs["conversation_id"] == conv_id
    assert kwargs["user_id"] == "gh-user-test"
    assert str(conv_id) in message
    assert "please continue the task" in message


def test_trims_and_rejects_whitespace_only_content_without_persisting(client):
    test_client, db_url = client
    guild_id = "g-cm-trim"
    insert_guild(db_url, guild_id)
    conv_id = insert_conversation(db_url, guild_id, user_id="gh-user-test")
    token = make_auth_token(db_url)

    with patch.object(triggers, "trigger_foreman", new=AsyncMock()) as mock_trigger:
        resp = test_client.post(
            _url(guild_id, conv_id),
            json={"content": "\n\t  "},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 400
    mock_trigger.assert_not_called()
    with _sync_session(db_url) as session:
        count = (
            session.execute(select(Message).where(col(Message.conversation_id) == conv_id))
            .scalars()
            .all()
        )
    assert count == []


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------


def test_broadcasts_to_websocket(client):
    test_client, db_url = client
    guild_id = "g-cm-ws"
    insert_guild(db_url, guild_id)
    conv_id = insert_conversation(db_url, guild_id, user_id="gh-user-test")
    token = make_auth_token(db_url)

    with patch.object(triggers, "trigger_foreman", new=AsyncMock()):
        with test_client.websocket_connect(f"/ws/{guild_id}") as ws:
            resp = test_client.post(
                _url(guild_id, conv_id),
                json={"content": "broadcast me"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = ws.receive_json()

    assert data["type"] == "chat"
    assert data["content"] == "broadcast me"
    assert data["from"] == "user"
    assert data["to"] == "foreman"
    assert data["userId"] == "gh-user-test"
