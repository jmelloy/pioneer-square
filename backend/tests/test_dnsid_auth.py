"""Tests for DNSid A2A authentication and the A2A client.

All network calls and subprocess calls are mocked — no real I/O.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from foreman.auth import DNSidAuthScheme
from foreman.a2a_client import A2AClient, _guild_caller_domain

# ---------------------------------------------------------------------------
# DNSidAuthScheme
# ---------------------------------------------------------------------------


async def test_dnsid_auth_returns_authorization_header():
    """DNSidAuthScheme produces Authorization: DNSid <jwt>."""
    scheme = DNSidAuthScheme(
        caller_domain="testguild.pioneer-square.melloy.life",
        private_key_pem="fake-pem",
    )
    with (
        patch.object(
            scheme, "_challenge_sync", return_value={"nonce": "abc", "challenge_id": "ch-1"}
        ),
        patch("foreman.auth._dnsid_sign_sync", return_value="fake.jwt.token"),
    ):
        headers = await scheme.get_auth_headers("https://agent.example.com")

    assert headers == {"Authorization": "DNSid fake.jwt.token"}


async def test_dnsid_auth_passes_correct_claims():
    """JWT claims include nonce, challenge_id, purpose, iss, sub, iat, exp."""
    scheme = DNSidAuthScheme(
        caller_domain="g1.pioneer-square.melloy.life",
        private_key_pem="fake-pem",
        purpose="pr-review",
    )
    captured_claims: list[dict] = []

    def capture_sign(claims, private_key_pem):
        captured_claims.append(claims)
        return "signed.jwt.here"

    with (
        patch.object(
            scheme, "_challenge_sync", return_value={"nonce": "n42", "challenge_id": "c99"}
        ),
        patch("foreman.auth._dnsid_sign_sync", side_effect=capture_sign),
    ):
        await scheme.get_auth_headers("https://example.com")

    claims = captured_claims[0]
    assert claims["nonce"] == "n42"
    assert claims["challenge_id"] == "c99"
    assert claims["purpose"] == "pr-review"
    assert claims["iss"] == "g1.pioneer-square.melloy.life"
    assert claims["sub"] == "g1.pioneer-square.melloy.life"
    assert "iat" in claims
    assert "exp" in claims
    assert claims["exp"] > claims["iat"]


async def test_dnsid_auth_forwards_private_key_pem():
    """private_key_pem is passed through to _dnsid_sign_sync."""
    scheme = DNSidAuthScheme(
        caller_domain="x.pioneer-square.melloy.life",
        private_key_pem="my-pem-data",
    )
    captured: list = []

    def capture_sign(claims, private_key_pem):
        captured.append(private_key_pem)
        return "tok"

    with (
        patch.object(scheme, "_challenge_sync", return_value={"nonce": "n", "challenge_id": "c"}),
        patch("foreman.auth._dnsid_sign_sync", side_effect=capture_sign),
    ):
        await scheme.get_auth_headers("https://example.com")

    assert captured[0] == "my-pem-data"


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
    client = A2AClient("https://agent.example.com/.well-known/agent.json")
    scheme = client._resolve_auth_scheme(SAMPLE_CARD, "g.pioneer-square.melloy.life", "my-pem")
    assert isinstance(scheme, DNSidAuthScheme)
    assert scheme.caller_domain == "g.pioneer-square.melloy.life"
    assert scheme.private_key_pem == "my-pem"
    assert scheme.purpose == "pr-review"


def test_resolve_dnsid_scheme_raises_without_key():
    """_resolve_auth_scheme raises if agent requires DNSid but no key is provided."""
    client = A2AClient("https://agent.example.com/.well-known/agent.json")
    with pytest.raises(ValueError, match="no private key"):
        client._resolve_auth_scheme(SAMPLE_CARD, "g.pioneer-square.melloy.life", None)


def test_resolve_no_matching_scheme():
    """_resolve_auth_scheme returns None when card has no recognised auth scheme."""
    card = {**SAMPLE_CARD, "securitySchemes": {}, "security": []}
    client = A2AClient("https://example.com/.well-known/agent.json")
    assert client._resolve_auth_scheme(card, "x") is None


def test_guild_caller_domain_default():
    assert _guild_caller_domain("abc123") == "abc123.pioneer-square.melloy.life"


def test_guild_caller_domain_custom():
    assert _guild_caller_domain("abc123", "custom.example.com") == "abc123.custom.example.com"


async def test_review_pr_attaches_dnsid_header():
    """review_pr runs the challenge, attaches Authorization: DNSid, and POSTs."""
    client = A2AClient("https://agent.meyers.life/.well-known/agent.json")
    client._card = SAMPLE_CARD

    captured: dict = {}
    fake_task_result = {"task_id": "t-1", "status": "complete"}

    def fake_fetch(url, *, data=None, headers=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        captured["data"] = data
        return {"result": fake_task_result}

    with (
        patch("foreman.a2a_client._fetch_json", side_effect=fake_fetch),
        patch(
            "foreman.auth.DNSidAuthScheme._challenge_sync",
            return_value={"nonce": "n1", "challenge_id": "c1"},
        ),
        patch("foreman.auth._dnsid_sign_sync", return_value="signed.jwt.value"),
    ):
        result = await client.review_pr(
            "https://github.com/owner/repo/pull/42",
            caller_domain="guild1.pioneer-square.melloy.life",
            private_key_pem="fake-pem",
        )

    assert captured["headers"].get("Authorization") == "DNSid signed.jwt.value"
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

    with patch("foreman.a2a_client._fetch_json", side_effect=fake_fetch):
        await client.review_pr("https://github.com/o/r/pull/1")

    assert "Authorization" not in captured["headers"]
