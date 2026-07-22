"""Focused tests for the DNSid-authenticated inbound A2A boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from helpers import insert_guild


class _FakeSignatureProfile:
    def __init__(self, verified=None, error: Exception | None = None) -> None:
        self.verified = verified
        self.error = error

    def verify_signed_http_request(self, request, options):
        if self.error:
            raise self.error
        return self.verified


def _runtime(
    sender: str,
    *,
    guild_slug: str = "pioneer-a2a-1",
    allowed: bool = True,
    error: Exception | None = None,
):
    verified = SimpleNamespace(
        domain=sender,
        record=SimpleNamespace(gi="dev.dnsid.test"),
        dnssec_state=SimpleNamespace(name="VALID"),
        verified_at=datetime.now(UTC),
        cached_state=lambda: "ACTIVE",
    )
    profile = _FakeSignatureProfile(verified, error)
    return SimpleNamespace(
        guild_slug=guild_slug,
        allowlist=frozenset({sender} if allowed else {"other.dev.dnsid.test"}),
        public_url=lambda path, query="": f"https://pioneer.dev.dnsid.test{path}",
        verify_request=lambda method, url, headers, body: profile.verify_signed_http_request(
            None, None
        ),
    )


def _headers() -> dict[str, str]:
    return {
        "a2a-version": "1.0",
        "a2a-extensions": (
            "https://example-provider.example/a2a/extensions/dnsid-http-message-signatures/v1"
        ),
        "signature-input": (
            'a2a=("@method" "@authority" "@target-uri" "content-type" '
            '"content-digest" "a2a-version" "a2a-extensions");nonce="n-1"'
        ),
        "signature": "a2a=:ZmFrZQ==:",
    }


def _payload(message_id: str = "barnji-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "message/send",
        "id": 1,
        "params": {
            "skill": "pr-review",
            "message": {
                "messageId": message_id,
                "parts": [
                    {
                        "type": "github-pr-url",
                        "url": "https://github.com/org/repo/pull/42",
                    },
                    {"type": "text", "text": "Focus on replay handling."},
                ],
            },
        },
    }


def test_signed_authorized_request_is_accepted_and_replay_rejected(client):
    test_client, db_url = client
    insert_guild(db_url, "pioneer-a2a-1", name="Pioneer")
    runtime = _runtime("barnji.dev.dnsid.test")
    trigger = AsyncMock()
    with (
        patch("routes.a2a.get_dnsid_runtime", return_value=runtime),
        patch("ws_handlers._trigger_foreman", trigger),
    ):
        first = test_client.post("/", json=_payload(), headers=_headers())
        poll = test_client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "method": "tasks/get",
                "id": 2,
                "params": {"id": "barnji-1"},
            },
            headers=_headers(),
        )
        replay = test_client.post("/", json=_payload(), headers=_headers())

    assert first.status_code == 200
    assert "verified DNSid barnji.dev.dnsid.test" in first.json()["result"]["parts"][0]["text"]
    assert poll.status_code == 200
    assert poll.json()["result"]["status"]["state"] == "submitted"
    assert replay.status_code == 401
    assert replay.json()["error"]["message"] == "replayed A2A request"
    trigger.assert_awaited_once()


def test_real_dnsid_sdk_signature_is_accepted(client):
    pytest.importorskip(
        "dnsid",
        reason="dnsid-py is not published yet; install editable from ../dnsid-py to run this test",
    )
    from dnsid import JWKS, HttpRequest, LocalKeyProvider
    from dnsid.http_signatures import HttpSignatureProfile
    from dnsid.models import HttpSigningOptions
    from foreman.dnsid_identity import (
        DNSID_A2A_EXTENSION_URI,
        DNSID_A2A_SIGNATURE_TAG,
        DNSidRuntime,
    )

    test_client, db_url = client
    insert_guild(db_url, "pioneer-a2a-4", name="Pioneer")
    sender = "participant.dev.dnsid.test"
    sender_keys = LocalKeyProvider.generate()
    verified = SimpleNamespace(
        domain=sender,
        record=SimpleNamespace(gi="dev.dnsid.test"),
        jwks=JWKS(keys=[sender_keys.signing_key()]),
        dnssec_state=SimpleNamespace(name="VALID"),
        verified_at=datetime.now(UTC),
        cached_state=lambda: "ACTIVE",
    )
    verifier_resolver = MagicMock()
    verifier_resolver.verify_domain.return_value = verified
    verifier = HttpSignatureProfile(
        resolver=verifier_resolver,
        key_provider=LocalKeyProvider.generate(),
        domain="pioneer.dev.dnsid.test",
    )
    runtime = DNSidRuntime(
        guild_slug="pioneer-a2a-4",
        public_origin="https://pioneer.dev.dnsid.test",
        allowlist=frozenset({sender}),
        manager=verifier_resolver,
        http_signatures=verifier,
    )

    body = json.dumps(_payload("participant-1"), separators=(",", ":")).encode()
    unsigned = HttpRequest(
        method="POST",
        url="https://pioneer.dev.dnsid.test/",
        headers={
            "content-type": "application/json",
            "a2a-version": "1.0",
            "a2a-extensions": DNSID_A2A_EXTENSION_URI,
        },
        body=body,
    )
    signer = HttpSignatureProfile(
        resolver=MagicMock(),
        key_provider=sender_keys,
        domain=sender,
    )
    signed = signer.create_signed_http_request(
        unsigned,
        HttpSigningOptions(
            additional_components=["content-type", "a2a-version", "a2a-extensions"],
            tag=DNSID_A2A_SIGNATURE_TAG,
        ),
    )

    trigger = AsyncMock()
    with (
        patch("routes.a2a.get_dnsid_runtime", return_value=runtime),
        patch("ws_handlers._trigger_foreman", trigger),
    ):
        response = test_client.post("/", content=body, headers=signed.headers)

    assert response.status_code == 200
    assert (
        "verified DNSid participant.dev.dnsid.test" in response.json()["result"]["parts"][0]["text"]
    )
    verifier_resolver.verify_domain.assert_called_once_with(sender)
    trigger.assert_awaited_once()


def test_authenticated_but_unauthorized_dnsid_is_forbidden(client):
    test_client, db_url = client
    insert_guild(db_url, "pioneer-a2a-2", name="Pioneer")
    runtime = _runtime("barnji.dev.dnsid.test", guild_slug="pioneer-a2a-2", allowed=False)
    with patch("routes.a2a.get_dnsid_runtime", return_value=runtime):
        response = test_client.post("/", json=_payload("barnji-2"), headers=_headers())
    assert response.status_code == 403


def test_invalid_dnsid_signature_is_unauthorized(client):
    test_client, db_url = client
    insert_guild(db_url, "pioneer-a2a-3", name="Pioneer")
    runtime = _runtime(
        "barnji.dev.dnsid.test",
        guild_slug="pioneer-a2a-3",
        error=ValueError("bad digest"),
    )
    with patch("routes.a2a.get_dnsid_runtime", return_value=runtime):
        response = test_client.post("/", json=_payload("barnji-3"), headers=_headers())
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "DNSid authentication failed"
