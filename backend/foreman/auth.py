"""DNSid challenge-response authentication for A2A protocol connections.

When an A2A agent declares a dnsid-cr security scheme the caller must:
  1. POST dnsid.challenge {caller_domain, client_nonce} to receive
     {server_nonce, challenge_id, server_assertion, exp}.
  2. Sign a JWT (EdDSA/Ed25519) via the dnsid-py library with claims:
     iss/sub=caller_domain, aud=service_domain, nonce=server_nonce,
     challenge_id, purpose, iat, exp (≤60s), jti.
  3. Include Authorization: DNSid <jwt> on the message/stream call.

The caller's identity is a subdomain of the guild's base domain
(e.g. {guild_id}.pioneer-square.melloy.life), and the remote agent can
verify ownership by fetching {caller_domain}/.well-known/jwks.json.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.request
import uuid
from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# dnsid-py signing helper
# ---------------------------------------------------------------------------


def _dnsid_sign_sync(claims: dict, private_key_pem: str) -> str:
    """Sign a JWT with an Ed25519 PEM private key. Returns the compact JWT string."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    def _b64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    private_key = load_pem_private_key(private_key_pem.encode(), password=None)
    header = {"alg": "EdDSA", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = private_key.sign(signing_input)
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


# ---------------------------------------------------------------------------
# Auth scheme interface
# ---------------------------------------------------------------------------


class A2AAuthScheme(ABC):
    """Pluggable A2A authentication scheme."""

    @abstractmethod
    async def get_auth_headers(self, agent_base_url: str) -> dict[str, str]:
        """Perform any required handshake and return headers for task requests."""


# ---------------------------------------------------------------------------
# DNSid scheme
# ---------------------------------------------------------------------------


class DNSidAuthScheme(A2AAuthScheme):
    """Mutual DNSid challenge-response (dnsid-cr security scheme).

    Step 1 — POST dnsid.challenge {caller_domain, client_nonce} to /jsonrpc.
    Step 2 — Sign an EdDSA JWT with the returned server_nonce/challenge_id.
    Step 3 — Return Authorization: DNSid <jwt>.
    """

    def __init__(
        self,
        caller_domain: str,
        private_key_pem: str,
        purpose: str = "a2a-pr-review",
        service_domain: str | None = None,
    ) -> None:
        self.caller_domain = caller_domain
        self.private_key_pem = private_key_pem
        self.purpose = purpose
        self.service_domain = service_domain  # aud claim; inferred from base_url if None

    def _challenge_sync(self, agent_base_url: str) -> dict:
        """Synchronous POST to dnsid.challenge; returns unwrapped result dict."""
        import secrets as _secrets

        url = agent_base_url.rstrip("/") + "/jsonrpc"
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "dnsid.challenge",
                "params": {
                    "caller_domain": self.caller_domain,
                    "client_nonce": _secrets.token_hex(16),
                },
                "id": 1,
            }
        ).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = data.get("result", data)
        if "error" in data and "server_nonce" not in result:
            raise RuntimeError(
                f"dnsid.challenge failed: {data['error'].get('message', data['error'])}"
            )
        return result

    async def get_auth_headers(self, agent_base_url: str) -> dict[str, str]:
        result = await asyncio.to_thread(self._challenge_sync, agent_base_url)
        server_nonce = result["server_nonce"]
        challenge_id = result["challenge_id"]

        from urllib.parse import urlparse

        aud = self.service_domain or urlparse(agent_base_url).hostname or agent_base_url

        now = int(time.time())
        claims = {
            "iss": self.caller_domain,
            "sub": self.caller_domain,
            "aud": aud,
            "nonce": server_nonce,
            "challenge_id": challenge_id,
            "purpose": self.purpose,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 60,
        }
        token = await asyncio.to_thread(_dnsid_sign_sync, claims, self.private_key_pem)
        return {"Authorization": f"DNSid {token}"}
