"""Tests for the /api/push/register endpoints and the push dispatcher.

Covers the auth gate, idempotent re-registration, ownership-scoped deletion,
the listing endpoint's token-suffix redaction, and the dispatcher behaviour
when APNs is not configured (the only path exercised in CI — actual APNs
HTTP/2 calls require a real Apple key and aren't part of the suite).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import _sync_session, make_auth_token
from models import PushToken, User
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert


def _ensure_user(db_url: str, user_id: str) -> None:
    """Create a users row so PushToken's FK to users.id resolves.

    The default ``make_auth_token`` helper only seeds ``github_tokens`` and
    ``user_sessions``; production OAuth populates ``users`` as well, but the
    test helper doesn't and won't be changed in this PR.
    """
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.execute(
            pg_insert(User)
            .values(
                id=user_id,
                github_id=user_id,
                github_login=user_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.commit()


def _login(db_url: str, user_id: str = "gh-user-test", username: str = "testuser") -> str:
    """Seed a logged-in user (auth token + users row) and return the bearer."""
    token = make_auth_token(db_url, user_id=user_id, username=username)
    _ensure_user(db_url, user_id)
    return token


def _seed_token(db_url: str, token: str, user_id: str = "gh-user-test") -> None:
    _ensure_user(db_url, user_id)
    now = datetime.now(UTC)
    with _sync_session(db_url) as session:
        session.add(
            PushToken(
                token=token,
                user_id=user_id,
                platform="ios",
                created_at=now,
                last_seen_at=now,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# POST /api/push/register
# ---------------------------------------------------------------------------


def test_register_requires_auth(client):
    test_client, _ = client
    resp = test_client.post("/api/push/register", json={"token": "abc"})
    assert resp.status_code == 401


def test_register_rejects_unknown_platform(client):
    test_client, db_url = client
    token = _login(db_url)
    resp = test_client.post(
        "/api/push/register",
        json={"token": "device-tok-1", "platform": "android"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_register_persists_row(client):
    test_client, db_url = client
    token = _login(db_url)
    resp = test_client.post(
        "/api/push/register",
        json={"token": "device-tok-2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    with _sync_session(db_url) as session:
        row = session.scalar(select(PushToken).where(PushToken.token == "device-tok-2"))
        assert row is not None
        assert row.user_id == "gh-user-test"
        assert row.platform == "ios"


def test_register_is_idempotent_and_bumps_last_seen(client):
    test_client, db_url = client
    token = _login(db_url)
    headers = {"Authorization": f"Bearer {token}"}

    resp = test_client.post("/api/push/register", json={"token": "device-tok-3"}, headers=headers)
    assert resp.status_code == 200
    with _sync_session(db_url) as session:
        first_seen = session.scalar(
            select(PushToken.last_seen_at).where(PushToken.token == "device-tok-3")
        )

    # Force enough wall-clock drift that the second timestamp differs.
    import time

    time.sleep(0.01)
    resp = test_client.post("/api/push/register", json={"token": "device-tok-3"}, headers=headers)
    assert resp.status_code == 200
    with _sync_session(db_url) as session:
        second_seen = session.scalar(
            select(PushToken.last_seen_at).where(PushToken.token == "device-tok-3")
        )
        rows = session.scalars(select(PushToken).where(PushToken.token == "device-tok-3")).all()
    assert len(rows) == 1, "re-register should not insert a duplicate row"
    assert second_seen >= first_seen


def test_register_moves_token_between_users(client):
    """Apple recycles tokens when a device is signed into a new account.
    The latest user wins so the previous owner stops receiving the device's
    notifications."""
    test_client, db_url = client
    alice = _login(db_url, user_id="alice", username="alice")
    bob = _login(db_url, user_id="bob", username="bob")

    test_client.post(
        "/api/push/register",
        json={"token": "device-tok-4"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    test_client.post(
        "/api/push/register",
        json={"token": "device-tok-4"},
        headers={"Authorization": f"Bearer {bob}"},
    )

    with _sync_session(db_url) as session:
        row = session.scalar(select(PushToken).where(PushToken.token == "device-tok-4"))
        assert row is not None
        assert row.user_id == "bob"


# ---------------------------------------------------------------------------
# DELETE /api/push/register
# ---------------------------------------------------------------------------


def test_unregister_removes_own_token(client):
    test_client, db_url = client
    token = _login(db_url)
    _seed_token(db_url, "device-tok-5", user_id="gh-user-test")

    resp = test_client.request(
        "DELETE",
        "/api/push/register",
        json={"token": "device-tok-5"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    with _sync_session(db_url) as session:
        row = session.scalar(select(PushToken).where(PushToken.token == "device-tok-5"))
        assert row is None


def test_unregister_does_not_touch_other_users_tokens(client):
    test_client, db_url = client
    _login(db_url, user_id="alice2", username="alice2")
    bob_session = _login(db_url, user_id="bob2", username="bob2")
    _seed_token(db_url, "device-tok-6", user_id="alice2")

    resp = test_client.request(
        "DELETE",
        "/api/push/register",
        json={"token": "device-tok-6"},
        headers={"Authorization": f"Bearer {bob_session}"},
    )
    # 200 even when nothing matched — deletion is idempotent and we don't
    # want to leak whether a token exists.
    assert resp.status_code == 200
    with _sync_session(db_url) as session:
        row = session.scalar(select(PushToken).where(PushToken.token == "device-tok-6"))
        assert row is not None, "Bob should not be able to delete Alice's token"


# ---------------------------------------------------------------------------
# GET /api/push/tokens
# ---------------------------------------------------------------------------


def test_list_tokens_returns_only_own_devices_with_redacted_suffix(client):
    test_client, db_url = client
    session_token = _login(db_url, user_id="carol", username="carol")
    _login(db_url, user_id="dave", username="dave")
    _seed_token(db_url, "carol-device-aaaaaaaa", user_id="carol")
    _seed_token(db_url, "dave-device-bbbbbbbb", user_id="dave")

    resp = test_client.get(
        "/api/push/tokens",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 200
    tokens = resp.json()["tokens"]
    assert len(tokens) == 1
    assert tokens[0]["token_suffix"] == "aaaaaaaa"
    # Full token must not leak.
    assert "carol-device" not in resp.text


# ---------------------------------------------------------------------------
# Dispatcher (no-APNs path)
# ---------------------------------------------------------------------------


def test_send_to_user_logs_and_returns_zero_when_apns_unconfigured(client, monkeypatch, caplog):
    test_client, db_url = client
    _login(db_url, user_id="eve", username="eve")
    _seed_token(db_url, "eve-device-cccccccc", user_id="eve")

    for var in ("APNS_KEY_P8", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID"):
        monkeypatch.delenv(var, raising=False)

    from push import send_to_user

    with caplog.at_level("INFO", logger="push"):
        delivered = asyncio.run(send_to_user("eve", title="hi", body="there"))

    assert delivered == 0
    assert any("APNs not configured" in rec.message for rec in caplog.records)


def test_send_to_user_returns_zero_when_no_devices(client, monkeypatch):
    test_client, db_url = client
    _login(db_url, user_id="frank", username="frank")
    for var in ("APNS_KEY_P8", "APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID"):
        monkeypatch.delenv(var, raising=False)

    from push import send_to_user

    delivered = asyncio.run(send_to_user("frank", title="hi", body="there"))
    assert delivered == 0


@pytest.mark.skipif(
    not os.environ.get("APNS_LIVE_TEST"),
    reason="Needs real APNs credentials; set APNS_LIVE_TEST=1 and APNS_* vars to run.",
)
def test_send_to_user_signs_jwt_with_real_credentials():  # pragma: no cover
    """Smoke test for the JWT signing path — only runs when explicitly
    enabled via APNS_LIVE_TEST=1. Not part of CI."""
    from push import _load_config, _make_jwt

    config = _load_config()
    assert config is not None
    jwt = _make_jwt(config)
    assert jwt.count(".") == 2
