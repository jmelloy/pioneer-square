"""DNSid challenge-response authentication for A2A protocol connections.

When an A2A agent declares a dnsid-based security scheme the caller must:
  1. POST dnsid.challenge to the agent to receive a server {nonce, challenge_id}.
  2. Sign a JWT (ES256) containing those values plus purpose/iss/sub.
  3. Include Authorization: DNSid <jwt> on subsequent task requests.

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
from abc import ABC, abstractmethod

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

# ---------------------------------------------------------------------------
# JWT primitives
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def build_es256_jwt(claims: dict, private_key_pem: str, key_id: str) -> str:
    """Return a signed compact ES256 JWT."""
    header = _b64url(
        json.dumps({"alg": "ES256", "typ": "JWT", "kid": key_id}, separators=(",", ":")).encode()
    )
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    der_sig = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))  # type: ignore[arg-type]

    # Convert DER-encoded ECDSA sig to fixed-length r||s (32+32 bytes for P-256)
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{payload}.{_b64url(raw_sig)}"


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

    Step 1 — POST dnsid.challenge (JSON-RPC 2.0) to {base_url}/a2a.
    Step 2 — Build an ES256 JWT with nonce/challenge_id/purpose/iss/sub.
    Step 3 — Return Authorization: DNSid <jwt>.
    """

    def __init__(
        self,
        caller_domain: str,
        private_key_pem: str,
        key_id: str,
        purpose: str = "a2a-pr-review",
    ) -> None:
        self.caller_domain = caller_domain
        self.private_key_pem = private_key_pem
        self.key_id = key_id
        self.purpose = purpose

    def _challenge_sync(self, agent_base_url: str) -> dict:
        """Synchronous POST to dnsid.challenge; returns unwrapped result dict."""
        url = agent_base_url.rstrip("/") + "/a2a"
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "dnsid.challenge",
                "params": {"caller_id": self.caller_domain},
                "id": 1,
            }
        ).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        # Unwrap JSON-RPC envelope if present
        return data.get("result", data)

    async def get_auth_headers(self, agent_base_url: str) -> dict[str, str]:
        result = await asyncio.to_thread(self._challenge_sync, agent_base_url)
        nonce = result["nonce"]
        challenge_id = result["challenge_id"]

        now = int(time.time())
        claims = {
            "iss": self.caller_domain,
            "sub": self.caller_domain,
            "nonce": nonce,
            "challenge_id": challenge_id,
            "purpose": self.purpose,
            "iat": now,
            "exp": now + 300,
        }
        token = build_es256_jwt(claims, self.private_key_pem, self.key_id)
        return {"Authorization": f"DNSid {token}"}
