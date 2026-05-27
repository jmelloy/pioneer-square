"""Tests for OIDC authentication and A2A receiver enablement.

All network calls are mocked — no real HTTP is issued.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from foreman.oidc import (
    OIDCConfig,
    _b64url_decode,
    _decode_jwt_parts,
    _verify_ed25519,
    is_a2a_receiver_enabled,
    validate_oidc_token,
)
from helpers import insert_guild, make_auth_token

# ---------------------------------------------------------------------------
# Helpers — create Ed25519 keys and sign JWTs without an external library
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_ed25519_keypair():
    """Return (private_key, public_key) as cryptography objects."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _public_key_to_jwk(pub, kid: str = "test-key-1") -> dict:
    from cryptography.hazmat.primitives import serialization

    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw), "kid": kid}


def _make_jwt(payload: dict, priv, kid: str = "test-key-1") -> str:
    """Produce a compact EdDSA JWT signed with *priv*."""
    header = {"alg": "EdDSA", "kid": kid}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    sig = priv.sign(signing_input)
    return f"{h}.{p}.{_b64url(sig)}"


# ---------------------------------------------------------------------------
# is_a2a_receiver_enabled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_val, expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        (None, False),
    ],
)
def test_is_a2a_receiver_enabled(env_val, expected, monkeypatch):
    if env_val is None:
        monkeypatch.delenv("A2A_RECEIVER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("A2A_RECEIVER_ENABLED", env_val)
    assert is_a2a_receiver_enabled() == expected


# ---------------------------------------------------------------------------
# OIDCConfig.from_env
# ---------------------------------------------------------------------------


def test_oidc_config_defaults(monkeypatch):
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
    cfg = OIDCConfig.from_env()
    assert cfg.issuer is None
    assert cfg.audience == "pioneer-square"


def test_oidc_config_from_env(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "my-service")
    cfg = OIDCConfig.from_env()
    assert cfg.issuer == "https://auth.example.com"
    assert cfg.audience == "my-service"


def test_oidc_config_empty_issuer_treated_as_none(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "")
    cfg = OIDCConfig.from_env()
    assert cfg.issuer is None


# ---------------------------------------------------------------------------
# JWT parsing helpers
# ---------------------------------------------------------------------------


def test_decode_jwt_parts_rejects_non_jwt():
    with pytest.raises(ValueError, match="3 dot-separated"):
        _decode_jwt_parts("not.a.valid.jwt.token.with.too.many.parts")


def test_decode_jwt_parts_rejects_bad_base64():
    with pytest.raises(ValueError):
        _decode_jwt_parts("!!!!.!!!!.!!!!")


def test_decode_jwt_parts_returns_correct_types():
    priv, pub = _make_ed25519_keypair()
    payload = {"sub": "u1", "iss": "https://example.com", "exp": int(time.time()) + 300}
    token = _make_jwt(payload, priv)
    header, claims, signing_input, signature = _decode_jwt_parts(token)
    assert header["alg"] == "EdDSA"
    assert claims["sub"] == "u1"
    assert isinstance(signing_input, bytes)
    assert isinstance(signature, bytes)


# ---------------------------------------------------------------------------
# validate_oidc_token — success paths
# ---------------------------------------------------------------------------


def test_validate_oidc_token_valid_ed25519(monkeypatch):
    """A correctly signed EdDSA token passes validation."""
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "agent-x",
        "iss": "https://auth.example.com",
        "aud": "pioneer-square",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer="https://auth.example.com", audience="pioneer-square")

    with patch("foreman.oidc._fetch_jwks", return_value=[jwk]):
        result = validate_oidc_token(token, cfg)

    assert result["sub"] == "agent-x"
    assert result["iss"] == "https://auth.example.com"


def test_validate_oidc_token_any_issuer_when_config_issuer_none(monkeypatch):
    """When config.issuer is None, any iss value is accepted."""
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "u2",
        "iss": "https://whatever.example.com",
        "aud": "pioneer-square",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with patch("foreman.oidc._fetch_jwks", return_value=[jwk]):
        result = validate_oidc_token(token, cfg)

    assert result["sub"] == "u2"


def test_validate_oidc_token_audience_as_list(monkeypatch):
    """Token aud as a list containing the expected audience passes."""
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "u3",
        "iss": "https://auth.example.com",
        "aud": ["other-service", "pioneer-square"],
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with patch("foreman.oidc._fetch_jwks", return_value=[jwk]):
        result = validate_oidc_token(token, cfg)

    assert result["sub"] == "u3"


def test_validate_oidc_token_no_aud_claim_passes(monkeypatch):
    """Token without an aud claim skips the audience check."""
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "u4",
        "iss": "https://auth.example.com",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with patch("foreman.oidc._fetch_jwks", return_value=[jwk]):
        result = validate_oidc_token(token, cfg)

    assert result["sub"] == "u4"


# ---------------------------------------------------------------------------
# validate_oidc_token — failure paths
# ---------------------------------------------------------------------------


def test_validate_rejects_expired_token():
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "u5",
        "iss": "https://auth.example.com",
        "exp": int(time.time()) - 10,  # already expired
    }
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with (
        patch("foreman.oidc._fetch_jwks", return_value=[jwk]),
        pytest.raises(ValueError, match="expired"),
    ):
        validate_oidc_token(token, cfg)


def test_validate_rejects_wrong_issuer():
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "u6",
        "iss": "https://wrong.example.com",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer="https://correct.example.com", audience="pioneer-square")

    with (
        patch("foreman.oidc._fetch_jwks", return_value=[jwk]),
        pytest.raises(ValueError, match="Unexpected issuer"),
    ):
        validate_oidc_token(token, cfg)


def test_validate_rejects_wrong_audience():
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "u7",
        "iss": "https://auth.example.com",
        "aud": "other-service",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with (
        patch("foreman.oidc._fetch_jwks", return_value=[jwk]),
        pytest.raises(ValueError, match="Audience"),
    ):
        validate_oidc_token(token, cfg)


def test_validate_rejects_invalid_signature():
    priv, pub = _make_ed25519_keypair()
    priv2, _pub2 = _make_ed25519_keypair()
    # Sign with priv but validate against pub (which is paired with priv, not priv2)
    # Corrupt the signature by signing with a different key
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "u8",
        "iss": "https://auth.example.com",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv2)  # signed with wrong key
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with (
        patch("foreman.oidc._fetch_jwks", return_value=[jwk]),
        pytest.raises(ValueError, match="[Ii]nvalid"),
    ):
        validate_oidc_token(token, cfg)


def test_validate_rejects_missing_iss():
    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {"sub": "u9", "exp": int(time.time()) + 300}  # no iss
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with (
        patch("foreman.oidc._fetch_jwks", return_value=[jwk]),
        pytest.raises(ValueError, match="'iss'"),
    ):
        validate_oidc_token(token, cfg)


def test_validate_rejects_jwks_fetch_failure():
    priv, _ = _make_ed25519_keypair()
    payload = {"sub": "u10", "iss": "https://auth.example.com", "exp": int(time.time()) + 300}
    token = _make_jwt(payload, priv)
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with (
        patch("foreman.oidc._fetch_jwks", side_effect=OSError("connection refused")),
        pytest.raises(ValueError, match="Could not fetch JWKS"),
    ):
        validate_oidc_token(token, cfg)


def test_validate_rejects_unsupported_algorithm():
    """Tokens using RS256 (not EdDSA) are rejected."""
    # Build a fake RS256-headered JWT (signature won't be real but we'll mock JWKS)
    header = {"alg": "RS256", "kid": "k1"}
    payload = {"sub": "u11", "iss": "https://auth.example.com", "exp": int(time.time()) + 300}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    token = f"{h}.{p}.fakesig"
    cfg = OIDCConfig(issuer=None, audience="pioneer-square")

    with (
        patch("foreman.oidc._fetch_jwks", return_value=[{"kid": "k1", "kty": "RSA"}]),
        pytest.raises(ValueError, match="Unsupported"),
    ):
        validate_oidc_token(token, cfg)


# ---------------------------------------------------------------------------
# AgentCard security scheme inclusion
# ---------------------------------------------------------------------------


def test_agent_card_no_oidc_scheme_when_receiver_disabled(client, monkeypatch):
    """When A2A_RECEIVER_ENABLED is unset, securitySchemes is absent from the card."""
    monkeypatch.delenv("A2A_RECEIVER_ENABLED", raising=False)
    test_client, _ = client
    resp = test_client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "securitySchemes" not in data
    assert "security" not in data


def test_agent_card_includes_oidc_scheme_when_receiver_enabled(client, monkeypatch):
    """When A2A_RECEIVER_ENABLED=true, securitySchemes contains the oidc entry."""
    monkeypatch.setenv("A2A_RECEIVER_ENABLED", "true")
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    test_client, _ = client
    resp = test_client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "securitySchemes" in data
    assert "oidc" in data["securitySchemes"]
    oidc_scheme = data["securitySchemes"]["oidc"]
    assert oidc_scheme["type"] == "openIdConnect"
    assert "openIdConnectUrl" in oidc_scheme
    assert "security" in data
    assert data["security"] == [{"oidc": []}]


def test_agent_card_oidc_uses_configured_issuer(client, monkeypatch):
    """When OIDC_ISSUER is set, openIdConnectUrl is derived from it."""
    monkeypatch.setenv("A2A_RECEIVER_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    test_client, _ = client
    resp = test_client.get("/.well-known/agent.json")
    assert resp.status_code == 200
    data = resp.json()
    oidc_url = data["securitySchemes"]["oidc"]["openIdConnectUrl"]
    assert oidc_url.startswith("https://auth.example.com")


# ---------------------------------------------------------------------------
# authorize_worker_or_member — OIDC path (integration with auth_deps)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oidc_token_accepted_when_receiver_enabled(monkeypatch):
    """authorize_worker_or_member returns oidc:<sub> for a valid OIDC token."""
    from unittest.mock import AsyncMock, MagicMock

    import auth_deps

    monkeypatch.setenv("A2A_RECEIVER_ENABLED", "true")
    monkeypatch.setenv("OIDC_AUDIENCE", "pioneer-square")
    monkeypatch.delenv("OIDC_ISSUER", raising=False)

    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "external-agent-1",
        "iss": "https://auth.example.com",
        "aud": "pioneer-square",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)

    # exec() is awaited; one_or_none() is synchronous on the result.
    # Use MagicMock for the exec result so one_or_none returns None (not a coroutine).
    exec_result = MagicMock()
    exec_result.one_or_none.return_value = None

    mock_db = AsyncMock()
    mock_db.exec.return_value = exec_result

    async def fake_get_db():
        return mock_db

    monkeypatch.setattr("auth_deps.get_db", fake_get_db)
    monkeypatch.setattr("auth_deps.get_guild_pk", AsyncMock(return_value=1))

    with patch("foreman.oidc._fetch_jwks", return_value=[jwk]):
        result = await auth_deps.authorize_worker_or_member("g-test", token)

    assert result == "oidc:external-agent-1"


@pytest.mark.asyncio
async def test_oidc_token_rejected_when_receiver_disabled(monkeypatch):
    """When A2A_RECEIVER_ENABLED is off, OIDC tokens are rejected with 401."""
    from unittest.mock import AsyncMock, MagicMock

    import auth_deps
    from fastapi import HTTPException

    monkeypatch.delenv("A2A_RECEIVER_ENABLED", raising=False)

    priv, pub = _make_ed25519_keypair()
    jwk = _public_key_to_jwk(pub)
    payload = {
        "sub": "external-agent-2",
        "iss": "https://auth.example.com",
        "aud": "pioneer-square",
        "exp": int(time.time()) + 300,
    }
    token = _make_jwt(payload, priv)

    exec_result = MagicMock()
    exec_result.one_or_none.return_value = None

    mock_db = AsyncMock()
    mock_db.exec.return_value = exec_result

    async def fake_get_db():
        return mock_db

    monkeypatch.setattr("auth_deps.get_db", fake_get_db)
    monkeypatch.setattr("auth_deps.get_guild_pk", AsyncMock(return_value=1))

    with (
        patch("foreman.oidc._fetch_jwks", return_value=[jwk]),
        pytest.raises(HTTPException) as exc_info,
    ):
        await auth_deps.authorize_worker_or_member("g-test", token)

    assert exc_info.value.status_code == 401
