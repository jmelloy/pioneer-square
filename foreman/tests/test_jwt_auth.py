"""Unit tests for pioneer_foreman.jwt_auth."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from pioneer_foreman.jwt_auth import (
    _REFRESH_BUFFER,
    JWTTokenManager,
    _exp_from_jwt,
    make_jwt,
)

# ── make_jwt ──────────────────────────────────────────────────────────────


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without signature verification."""
    parts = token.split(".")
    assert len(parts) == 3, f"Expected 3 parts, got {len(parts)}"
    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def _decode_jwt_header(token: str) -> dict:
    parts = token.split(".")
    padded = parts[0] + "=" * (4 - len(parts[0]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_make_jwt_structure():
    token = make_jwt("abc123", "secret")
    parts = token.split(".")
    assert len(parts) == 3, "JWT must have three dot-separated parts"


def test_make_jwt_header():
    token = make_jwt("abc123", "secret")
    header = _decode_jwt_header(token)
    assert header["alg"] == "HS256"
    assert header["typ"] == "JWT"


def test_make_jwt_claims():
    guild_id = "myguild"
    before = int(time.time())
    token = make_jwt(guild_id, "secret")
    after = int(time.time())

    payload = _decode_jwt_payload(token)
    assert payload["sub"] == "pioneer-foreman"
    assert payload["guild_id"] == guild_id
    assert before <= payload["iat"] <= after
    assert payload["exp"] > payload["iat"]


def test_make_jwt_default_ttl():
    token = make_jwt("g", "s")
    payload = _decode_jwt_payload(token)
    assert payload["exp"] - payload["iat"] == 3600  # default TTL


def test_make_jwt_custom_ttl():
    token = make_jwt("g", "s", ttl=120)
    payload = _decode_jwt_payload(token)
    assert payload["exp"] - payload["iat"] == 120


def test_make_jwt_signature_validates():
    """Verify the signature by re-computing it with the known secret."""
    secret = "testsecret"
    token = make_jwt("g", secret)
    header_b64, payload_b64, sig_b64 = token.split(".")
    signing_input = f"{header_b64}.{payload_b64}"
    expected_sig = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    assert sig_b64 == expected_sig


def test_make_jwt_wrong_secret_has_different_sig():
    token_a = make_jwt("g", "secret-a")
    token_b = make_jwt("g", "secret-b")
    sig_a = token_a.split(".")[2]
    sig_b = token_b.split(".")[2]
    assert sig_a != sig_b


# ── _exp_from_jwt ─────────────────────────────────────────────────────────


def test_exp_from_jwt_extracts_expiry():
    token = make_jwt("g", "s", ttl=300)
    exp = _exp_from_jwt(token)
    assert exp > int(time.time())
    assert exp <= int(time.time()) + 300 + 2  # small clock tolerance


def test_exp_from_jwt_malformed_returns_zero():
    assert _exp_from_jwt("not.a.jwt") == 0


def test_exp_from_jwt_wrong_part_count_returns_zero():
    assert _exp_from_jwt("onlyone") == 0
    assert _exp_from_jwt("only.two") == 0


def test_exp_from_jwt_empty_returns_zero():
    assert _exp_from_jwt("") == 0


# ── JWTTokenManager ───────────────────────────────────────────────────────


def test_token_manager_returns_valid_token():
    mgr = JWTTokenManager("g", "secret")
    token = mgr.token()
    parts = token.split(".")
    assert len(parts) == 3


def test_token_manager_caches_token():
    mgr = JWTTokenManager("g", "secret")
    t1 = mgr.token()
    t2 = mgr.token()
    assert t1 == t2, "Second call should return the cached token"


def test_token_manager_refreshes_when_near_expiry(monkeypatch):
    """When the cached token is within REFRESH_BUFFER of expiry, a new one is minted."""
    mint_calls: list = []
    import pioneer_foreman.jwt_auth as jwt_mod

    original_make_jwt = jwt_mod.make_jwt

    def counting_make_jwt(*args, **kwargs):
        mint_calls.append(1)
        return original_make_jwt(*args, **kwargs)

    monkeypatch.setattr(jwt_mod, "make_jwt", counting_make_jwt)

    mgr = JWTTokenManager("g", "secret", ttl=3600)
    _ = mgr.token()  # first mint
    assert len(mint_calls) == 1

    # Simulate near-expiry: _exp is within the refresh buffer
    mgr._exp = int(time.time()) + _REFRESH_BUFFER - 1

    _ = mgr.token()  # should trigger a second mint
    assert len(mint_calls) == 2, "Manager should have called make_jwt again on near-expiry"


def test_token_manager_does_not_refresh_when_not_near_expiry():
    mgr = JWTTokenManager("g", "secret", ttl=3600)
    t1 = mgr.token()
    # Token is fresh, well before the refresh buffer
    t2 = mgr.token()
    assert t1 == t2


def test_token_manager_guild_id_in_token():
    mgr = JWTTokenManager("myguild", "secret")
    token = mgr.token()
    payload = _decode_jwt_payload(token)
    assert payload["guild_id"] == "myguild"


def test_token_manager_initial_state_forces_mint():
    """Fresh manager with no cached token always mints on first call."""
    mgr = JWTTokenManager("g", "secret")
    assert mgr._cached is None
    token = mgr.token()
    assert token is not None
    assert mgr._cached == token
