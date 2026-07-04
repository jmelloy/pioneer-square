"""Integration tests for the Discord slash command interaction endpoint.

Tests use mocked Ed25519 keys — no real Discord credentials are needed.
HTTP endpoint tests rely on the conftest ``client`` fixture (postgres-test DB).
Pure-unit tests run without any DB.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_keypair():
    """Return (private_key, public_key_hex)."""
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv, pub_bytes.hex()


def _sign(private_key, timestamp: str, body: bytes) -> str:
    """Hex Ed25519 signature of timestamp||body."""
    return private_key.sign(timestamp.encode() + body).hex()


@pytest.fixture()
def keypair():
    return _generate_keypair()


def _post_signed(c, priv, body: dict, timestamp: str = "1234567890"):
    raw = json.dumps(body).encode()
    sig = _sign(priv, timestamp, raw)
    return c.post(
        "/discord/interactions",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Signature-Ed25519": sig,
            "X-Signature-Timestamp": timestamp,
        },
    )


# ---------------------------------------------------------------------------
# Signature verification (pure unit)
# ---------------------------------------------------------------------------


def test_verify_signature_correct():
    from routes.discord import _verify_signature

    priv, pub_hex = _generate_keypair()
    body = b'{"type":1}'
    ts = "1700000000"
    sig = _sign(priv, ts, body)
    assert _verify_signature(pub_hex, sig, ts, body) is True


def test_verify_signature_tampered_body():
    from routes.discord import _verify_signature

    priv, pub_hex = _generate_keypair()
    body = b'{"type":1}'
    ts = "1700000000"
    sig = _sign(priv, ts, body)
    assert _verify_signature(pub_hex, sig, ts, b'{"type":2}') is False


def test_verify_signature_wrong_key():
    from routes.discord import _verify_signature

    priv, _ = _generate_keypair()
    _, other_pub = _generate_keypair()
    body = b'{"type":1}'
    ts = "1700000000"
    sig = _sign(priv, ts, body)
    assert _verify_signature(other_pub, sig, ts, body) is False


# ---------------------------------------------------------------------------
# URL parsers (pure unit)
# ---------------------------------------------------------------------------


def test_parse_issue_url_valid():
    from routes.discord import _parse_issue_url

    assert _parse_issue_url("https://github.com/my-org/my-repo/issues/42") == (
        "my-org/my-repo",
        42,
    )


def test_parse_issue_url_invalid():
    from routes.discord import _parse_issue_url

    assert _parse_issue_url("https://github.com/org/repo/pull/1") is None
    assert _parse_issue_url("not-a-url") is None


def test_parse_pr_url_valid():
    from routes.discord import _parse_pr_url

    assert _parse_pr_url("https://github.com/org/repo/pull/99") == ("org/repo", 99)


def test_parse_pr_url_invalid():
    from routes.discord import _parse_pr_url

    assert _parse_pr_url("https://github.com/org/repo/issues/1") is None


# ---------------------------------------------------------------------------
# Authorization (pure unit)
# ---------------------------------------------------------------------------


def test_is_authorized_no_restriction():
    """Empty DISCORD_ALLOWED_ROLE_IDS → allow all."""
    from routes.discord import _is_authorized

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DISCORD_ALLOWED_ROLE_IDS", None)
        assert _is_authorized({"member": {"roles": []}}) is True
        assert _is_authorized({}) is True


def test_is_authorized_matching_role(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_ROLE_IDS", "role-a,role-b")
    from routes.discord import _is_authorized

    assert _is_authorized({"member": {"roles": ["role-b", "role-c"]}}) is True


def test_is_authorized_no_matching_role(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_ROLE_IDS", "role-a,role-b")
    from routes.discord import _is_authorized

    assert _is_authorized({"member": {"roles": ["role-x"]}}) is False


def test_is_authorized_dm_denied_when_roles_required(monkeypatch):
    """DM interaction (no member) is denied when role restrictions are set."""
    monkeypatch.setenv("DISCORD_ALLOWED_ROLE_IDS", "some-role")
    from routes.discord import _is_authorized

    assert _is_authorized({}) is False


# ---------------------------------------------------------------------------
# HTTP endpoint tests (use conftest client with real DB)
# ---------------------------------------------------------------------------


def test_invalid_signature_returns_401(client, keypair, monkeypatch):
    """A wrong signature must return HTTP 401."""
    c, _ = client
    priv, pub_hex = keypair
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    body = json.dumps({"type": 1}).encode()
    resp = c.post(
        "/discord/interactions",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Signature-Ed25519": "deadbeef" * 8,
            "X-Signature-Timestamp": "1234567890",
        },
    )
    assert resp.status_code == 401


def test_missing_signature_headers_returns_401(client, keypair, monkeypatch):
    """Missing signature headers → HTTP 401."""
    c, _ = client
    _, pub_hex = keypair
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    body = json.dumps({"type": 1}).encode()
    resp = c.post("/discord/interactions", content=body)
    assert resp.status_code == 401


def test_valid_ping_returns_pong(client, keypair, monkeypatch):
    """Type-1 PING with a valid signature → type-1 PONG."""
    c, _ = client
    priv, pub_hex = keypair
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    resp = _post_signed(c, priv, {"type": 1})
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}


def test_no_public_key_skips_verification(client, monkeypatch):
    """When DISCORD_PUBLIC_KEY is absent, PING passes without verification."""
    c, _ = client
    monkeypatch.delenv("DISCORD_PUBLIC_KEY", raising=False)
    body = json.dumps({"type": 1}).encode()
    resp = c.post(
        "/discord/interactions", content=body, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}


def test_unauthorized_user_gets_ephemeral_denied(client, keypair, monkeypatch):
    """User without an allowed role receives ephemeral 'not authorized' message."""
    c, _ = client
    priv, pub_hex = keypair
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    monkeypatch.setenv("DISCORD_ALLOWED_ROLE_IDS", "allowed-role-1,allowed-role-2")
    interaction = {
        "type": 2,
        "token": "tok",
        "data": {
            "name": "ps",
            "type": 1,
            "options": [{"name": "status", "type": 1, "options": []}],
        },
        "member": {"roles": ["some-other-role"]},
    }
    resp = _post_signed(c, priv, interaction)
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == 4
    assert data["data"]["flags"] == 64
    assert "not authorized" in data["data"]["content"].lower()


def test_authorized_user_gets_deferred_ack(client, keypair, monkeypatch):
    """User with an allowed role gets a deferred ephemeral ack (type 5)."""
    c, _ = client
    priv, pub_hex = keypair
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    monkeypatch.setenv("DISCORD_ALLOWED_ROLE_IDS", "allowed-role")
    interaction = {
        "type": 2,
        "token": "tok",
        "data": {
            "name": "ps",
            "type": 1,
            "options": [{"name": "status", "type": 1, "options": []}],
        },
        "member": {"roles": ["allowed-role"]},
    }
    with patch("routes.discord._dispatch_command", new=AsyncMock()):
        resp = _post_signed(c, priv, interaction)
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == 5
    assert data["data"]["flags"] == 64


def test_no_roles_configured_allows_everyone(client, keypair, monkeypatch):
    """No DISCORD_ALLOWED_ROLE_IDS → anyone is allowed (deferred ack)."""
    c, _ = client
    priv, pub_hex = keypair
    monkeypatch.setenv("DISCORD_PUBLIC_KEY", pub_hex)
    monkeypatch.delenv("DISCORD_ALLOWED_ROLE_IDS", raising=False)
    interaction = {
        "type": 2,
        "token": "tok",
        "data": {
            "name": "ps",
            "type": 1,
            "options": [{"name": "status", "type": 1, "options": []}],
        },
        "member": {"roles": []},
    }
    with patch("routes.discord._dispatch_command", new=AsyncMock()):
        resp = _post_signed(c, priv, interaction)
    assert resp.status_code == 200
    assert resp.json()["type"] == 5


# ---------------------------------------------------------------------------
# _cmd_* unit tests — all DB calls mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_status_sends_embed(monkeypatch):
    """_cmd_status sends an embed with worker and task summary."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    guild = MagicMock()
    guild.id = 1

    w_online = MagicMock()
    w_online.state = "online"
    w_idle = MagicMock()
    w_idle.state = "idle"

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.exec = AsyncMock(
        side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=guild)),
            MagicMock(all=MagicMock(return_value=[w_online, w_idle])),
            MagicMock(all=MagicMock(return_value=[])),
        ]
    )

    sent = []

    async def fake_patch(path, payload):
        sent.append(payload)

    with (
        patch("routes.discord.AsyncSessionLocal", return_value=mock_db),
        patch("routes.discord._discord_patch", new=fake_patch),
    ):
        from routes.discord import _cmd_status

        await _cmd_status("tok-123", "test-guild")

    assert sent
    assert sent[0]["embeds"][0]["title"] == "Pioneer Square Status"


@pytest.mark.asyncio
async def test_cmd_workers_sends_embed(monkeypatch):
    """_cmd_workers lists all workers in an embed."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    guild = MagicMock()
    guild.id = 2

    row = MagicMock()
    row.id = "w-abc123"
    row.state = "online"
    row.repos = '["org/repo"]'
    row.name = "my-worker"
    row.org = None
    row.agent_count = 2

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.exec = AsyncMock(
        side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=guild)),
            MagicMock(all=MagicMock(return_value=[row])),
        ]
    )

    sent = []

    async def fake_patch(path, payload):
        sent.append(payload)

    with (
        patch("routes.discord.AsyncSessionLocal", return_value=mock_db),
        patch("routes.discord._discord_patch", new=fake_patch),
    ):
        from routes.discord import _cmd_workers

        await _cmd_workers("tok-w", "test-guild")

    assert sent
    assert "Workers" in sent[0]["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_cmd_pickup_creates_task_and_broadcasts(monkeypatch):
    """_cmd_pickup creates a Task row and broadcasts TaskCreatedMsg."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    guild = MagicMock()
    guild.id = 7

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=guild)))
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()

    sent = []
    broadcast_calls = []

    async def fake_patch(path, payload):
        sent.append(payload)

    async def fake_broadcast(guild_slug, msg):
        broadcast_calls.append((guild_slug, msg))

    with (
        patch("routes.discord.AsyncSessionLocal", return_value=mock_db),
        patch("routes.discord._discord_patch", new=fake_patch),
        patch("routes.discord.broadcast_msg", new=fake_broadcast),
    ):
        from routes.discord import _cmd_pickup

        await _cmd_pickup("tok-p", "test-guild", "https://github.com/org/repo/issues/10")

    assert mock_db.add.called
    assert mock_db.commit.called
    assert broadcast_calls
    assert sent
    assert "10" in sent[0]["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_cmd_pickup_invalid_url_sends_error(monkeypatch):
    """_cmd_pickup rejects invalid issue URLs without touching the DB."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    sent = []

    async def fake_patch(path, payload):
        sent.append(payload)

    with patch("routes.discord._discord_patch", new=fake_patch):
        from routes.discord import _cmd_pickup

        await _cmd_pickup("tok-bad", "test-guild", "not-a-url")

    assert sent
    assert "Invalid" in sent[0]["content"]


@pytest.mark.asyncio
async def test_cmd_review_creates_task(monkeypatch):
    """_cmd_review creates a review-phase Task row."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    guild = MagicMock()
    guild.id = 5

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.exec = AsyncMock(return_value=MagicMock(one_or_none=MagicMock(return_value=guild)))
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()

    sent = []

    async def fake_patch(path, payload):
        sent.append(payload)

    async def fake_broadcast(guild_slug, msg):
        pass

    with (
        patch("routes.discord.AsyncSessionLocal", return_value=mock_db),
        patch("routes.discord._discord_patch", new=fake_patch),
        patch("routes.discord.broadcast_msg", new=fake_broadcast),
    ):
        from routes.discord import _cmd_review

        await _cmd_review("tok-r", "test-guild", "https://github.com/org/repo/pull/99")

    assert mock_db.add.called
    added_task = mock_db.add.call_args[0][0]
    assert added_task.phase == "review"
    assert added_task.pr_number == 99
    assert sent
    assert "99" in sent[0]["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_cmd_cancel_cancels_task(monkeypatch):
    """_cmd_cancel sets task state to cancelled and broadcasts."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    guild = MagicMock()
    guild.id = 3

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.exec = AsyncMock(
        side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=guild)),
            MagicMock(one_or_none=MagicMock(return_value=("w-worker1", "working"))),
            MagicMock(),
        ]
    )
    mock_db.commit = AsyncMock()

    sent = []
    broadcast_calls = []

    async def fake_patch(path, payload):
        sent.append(payload)

    async def fake_broadcast(guild_slug, msg):
        broadcast_calls.append((guild_slug, msg))

    with (
        patch("routes.discord.AsyncSessionLocal", return_value=mock_db),
        patch("routes.discord._discord_patch", new=fake_patch),
        patch("routes.discord.broadcast_msg", new=fake_broadcast),
    ):
        from routes.discord import _cmd_cancel

        await _cmd_cancel("tok-c", "test-guild", "t-abc123")

    assert mock_db.commit.called
    assert len(broadcast_calls) == 2
    assert sent
    assert "t-abc123" in sent[0]["embeds"][0]["title"]


@pytest.mark.asyncio
async def test_cmd_cancel_already_done(monkeypatch):
    """_cmd_cancel returns informational message when task is already done."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "app-id")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")

    guild = MagicMock()
    guild.id = 3

    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=False)
    mock_db.exec = AsyncMock(
        side_effect=[
            MagicMock(one_or_none=MagicMock(return_value=guild)),
            MagicMock(one_or_none=MagicMock(return_value=("w-w1", "done"))),
        ]
    )

    sent = []

    async def fake_patch(path, payload):
        sent.append(payload)

    with (
        patch("routes.discord.AsyncSessionLocal", return_value=mock_db),
        patch("routes.discord._discord_patch", new=fake_patch),
    ):
        from routes.discord import _cmd_cancel

        await _cmd_cancel("tok-done", "test-guild", "t-done1")

    assert sent
    assert "already" in sent[0]["content"]
