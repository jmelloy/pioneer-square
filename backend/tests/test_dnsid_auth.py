"""Tests for DNSid A2A authentication and the A2A client.

All network calls are mocked — no real HTTP requests are made.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman.auth import DNSidAuthScheme, build_es256_jwt
from foreman.mcp_client import A2AClient, _guild_caller_domain

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ec_key():
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def _pem(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _decode_b64url(s: str) -> bytes:
    padded = s + "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(padded)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def test_build_es256_jwt_has_three_parts():
    key = _make_ec_key()
    token = build_es256_jwt({"sub": "test"}, _pem(key), "kid-1")
    assert len(token.split(".")) == 3


def test_build_es256_jwt_header():
    key = _make_ec_key()
    token = build_es256_jwt({"sub": "x"}, _pem(key), "my-kid")
    header = json.loads(_decode_b64url(token.split(".")[0]))
    assert header["alg"] == "ES256"
    assert header["typ"] == "JWT"
    assert header["kid"] == "my-kid"


def test_build_es256_jwt_payload_roundtrip():
    key = _make_ec_key()
    claims = {"sub": "testuser", "purpose": "a2a-pr-review", "exp": 9999999999}
    token = build_es256_jwt(claims, _pem(key), "k")
    payload = json.loads(_decode_b64url(token.split(".")[1]))
    assert payload == claims


def test_build_es256_jwt_signature_verifiable():
    """Signature produced by build_es256_jwt can be verified with the public key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    key = _make_ec_key()
    token = build_es256_jwt({"iss": "test"}, _pem(key), "k")
    header_b64, payload_b64, sig_b64 = token.split(".")

    raw_sig = _decode_b64url(sig_b64)
    assert len(raw_sig) == 64  # 32 bytes r + 32 bytes s
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    der_sig = encode_dss_signature(r, s)

    signing_input = f"{header_b64}.{payload_b64}".encode()
    key.public_key().verify(der_sig, signing_input, ec.ECDSA(hashes.SHA256()))


# ---------------------------------------------------------------------------
# DNSidAuthScheme
# ---------------------------------------------------------------------------


async def test_dnsid_auth_returns_authorization_header():
    """DNSidAuthScheme produces Authorization: DNSid <jwt>."""
    key = _make_ec_key()
    pem = _pem(key)
    scheme = DNSidAuthScheme(
        caller_domain="testguild.pioneer-square.melloy.life",
        private_key_pem=pem,
        key_id="test-key",
        purpose="a2a-pr-review",
    )
    with patch.object(
        scheme, "_challenge_sync", return_value={"nonce": "abc", "challenge_id": "ch-1"}
    ):
        headers = await scheme.get_auth_headers("https://agent.example.com")

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("DNSid ")


async def test_dnsid_auth_jwt_claims():
    """JWT payload contains nonce, challenge_id, purpose, iss, sub, iat, exp."""
    key = _make_ec_key()
    scheme = DNSidAuthScheme(
        caller_domain="g1.pioneer-square.melloy.life",
        private_key_pem=_pem(key),
        key_id="k",
        purpose="pr-review",
    )
    with patch.object(
        scheme, "_challenge_sync", return_value={"nonce": "n42", "challenge_id": "c99"}
    ):
        headers = await scheme.get_auth_headers("https://example.com")

    jwt = headers["Authorization"].split(" ", 1)[1]
    payload = json.loads(_decode_b64url(jwt.split(".")[1]))

    assert payload["nonce"] == "n42"
    assert payload["challenge_id"] == "c99"
    assert payload["purpose"] == "pr-review"
    assert payload["iss"] == "g1.pioneer-square.melloy.life"
    assert payload["sub"] == "g1.pioneer-square.melloy.life"
    assert "iat" in payload
    assert "exp" in payload
    assert payload["exp"] > payload["iat"]


async def test_dnsid_auth_jsonrpc_unwrap():
    """challenge_sync strips the JSON-RPC result wrapper if present."""
    key = _make_ec_key()
    scheme = DNSidAuthScheme(
        caller_domain="x.pioneer-square.melloy.life",
        private_key_pem=_pem(key),
        key_id="k1",
    )
    # _challenge_sync is already unwrapping; test that bare dict also works
    with patch.object(
        scheme, "_challenge_sync", return_value={"nonce": "bare-n", "challenge_id": "bare-c"}
    ):
        headers = await scheme.get_auth_headers("https://example.com")

    jwt = headers["Authorization"].split(" ", 1)[1]
    payload = json.loads(_decode_b64url(jwt.split(".")[1]))
    assert payload["nonce"] == "bare-n"
    assert payload["challenge_id"] == "bare-c"


# ---------------------------------------------------------------------------
# A2AClient
# ---------------------------------------------------------------------------

SAMPLE_CARD = {
    "name": "code-review-agent",
    "description": "PR code review.",
    "url": "http://127.0.0.1:8080",
    "version": "0.1.0",
    "capabilities": {"streaming": True},
    "securitySchemes": {
        "dnsid-cr": {
            "type": "custom",
            "scheme": "DNSid",
            "challengeMethod": "dnsid.challenge",
            "serviceDomain": "agent.meyers.life",
        }
    },
    "security": [{"dnsid-cr": []}],
    "skills": [{"id": "pr-review", "name": "Code review"}],
}


def test_agent_base_url_falls_back_to_card_host():
    """Loopback declared url → fall back to card URL's origin."""
    client = A2AClient("https://agent.meyers.life/.well-known/agent.json")
    assert client._agent_base_url(SAMPLE_CARD) == "https://agent.meyers.life"


def test_agent_base_url_uses_declared_url():
    """Non-loopback declared url → use it directly."""
    card = {**SAMPLE_CARD, "url": "https://real.example.com/agent"}
    client = A2AClient("https://other.example.com/.well-known/agent.json")
    assert client._agent_base_url(card) == "https://real.example.com/agent"


def test_resolve_dnsid_scheme():
    """_resolve_auth_scheme returns DNSidAuthScheme for a dnsid-cr card."""
    key = _make_ec_key()
    pem = _pem(key)
    client = A2AClient("https://agent.example.com/.well-known/agent.json")
    scheme = client._resolve_auth_scheme(SAMPLE_CARD, "g.pioneer-square.melloy.life", pem, "k1")
    assert isinstance(scheme, DNSidAuthScheme)
    assert scheme.caller_domain == "g.pioneer-square.melloy.life"
    assert scheme.purpose == "pr-review"


def test_resolve_no_matching_scheme():
    """_resolve_auth_scheme returns None when card has no recognised auth scheme."""
    card = {**SAMPLE_CARD, "securitySchemes": {}, "security": []}
    client = A2AClient("https://example.com/.well-known/agent.json")
    assert client._resolve_auth_scheme(card, "x", "pem", "k") is None


def test_guild_caller_domain_default():
    assert _guild_caller_domain("abc123") == "abc123.pioneer-square.melloy.life"


def test_guild_caller_domain_custom():
    assert _guild_caller_domain("abc123", "custom.example.com") == "abc123.custom.example.com"


async def test_review_pr_attaches_dnsid_header():
    """review_pr runs the challenge, attaches Authorization: DNSid, and POSTs."""
    key = _make_ec_key()
    pem = _pem(key)

    client = A2AClient("https://agent.meyers.life/.well-known/agent.json")
    client._card = SAMPLE_CARD  # skip network fetch

    captured: dict = {}
    fake_task_result = {"task_id": "t-1", "status": "complete"}

    def fake_fetch(url, *, data=None, headers=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["data"] = data
        return {"result": fake_task_result}

    with (
        patch("foreman.mcp_client._fetch_json", side_effect=fake_fetch),
        patch(
            "foreman.auth.DNSidAuthScheme._challenge_sync",
            return_value={"nonce": "n1", "challenge_id": "c1"},
        ),
    ):
        result = await client.review_pr(
            "https://github.com/owner/repo/pull/42",
            caller_domain="guild1.pioneer-square.melloy.life",
            private_key_pem=pem,
            key_id="k1",
        )

    assert "Authorization" in captured["headers"]
    assert captured["headers"]["Authorization"].startswith("DNSid ")
    assert captured["url"] == "https://agent.meyers.life/a2a"
    assert result == fake_task_result


async def test_review_pr_no_auth_when_no_scheme():
    """review_pr sends request without auth header when card has no auth scheme."""
    card_no_auth = {**SAMPLE_CARD, "securitySchemes": {}, "security": []}

    client = A2AClient("https://example.com/.well-known/agent.json")
    client._card = card_no_auth

    captured: dict = {}

    def fake_fetch(url, *, data=None, headers=None):
        captured["headers"] = dict(headers or {})
        return {"result": {}}

    with patch("foreman.mcp_client._fetch_json", side_effect=fake_fetch):
        await client.review_pr("https://github.com/o/r/pull/1")

    assert "Authorization" not in captured["headers"]
