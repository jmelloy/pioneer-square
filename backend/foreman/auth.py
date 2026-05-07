"""DNSid challenge-response authentication for A2A protocol connections.

When an A2A agent declares a dnsid-based security scheme the caller must:
  1. POST dnsid.challenge to the agent to receive a server {nonce, challenge_id}.
  2. Sign a JWT (EdDSA/Ed25519) via the dnsid-sdk CLI containing those values.
  3. Include Authorization: DNSid <jwt> on subsequent task requests.

The caller's identity is a subdomain of the guild's base domain
(e.g. {guild_id}.pioneer-square.melloy.life), and the remote agent can
verify ownership by fetching {caller_domain}/.well-known/jwks.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.request
from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# dnsid-sdk subprocess helpers
# ---------------------------------------------------------------------------


def _dnsid_bin() -> str:
    return os.path.expanduser(os.environ.get("DNSID_SDK_BIN", "~/dnsid-go/bin/dnsid-sdk"))


def _dnsid_sign_sync(claims: dict, private_key_pem: str) -> str:
    """Sign a JWT via `dnsid sign` using an Ed25519 PEM key. Returns the compact JWT string."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "key.pem")
        config_path = os.path.join(tmpdir, "config.json")
        with open(key_path, "w") as f:
            f.write(private_key_pem)
        with open(config_path, "w") as f:
            json.dump({"key_path": key_path}, f)
        result = subprocess.run(
            [_dnsid_bin(), "sign", "--config", config_path],
            input=json.dumps(claims).encode(),
            capture_output=True,
            timeout=10,
        )
    out = json.loads(result.stdout)
    if not out.get("ok"):
        raise RuntimeError(
            f"dnsid sign [{out.get('error', '?')}]: "
            f"{out.get('message', result.stderr.decode(errors='replace')[:200])}"
        )
    return out["jwt"]


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
    Step 2 — Sign an EdDSA JWT with nonce/challenge_id/purpose/iss/sub via dnsid-sdk.
    Step 3 — Return Authorization: DNSid <jwt>.
    """

    def __init__(
        self,
        caller_domain: str,
        private_key_pem: str,
        purpose: str = "a2a-pr-review",
    ) -> None:
        self.caller_domain = caller_domain
        self.private_key_pem = private_key_pem
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
        token = await asyncio.to_thread(_dnsid_sign_sync, claims, self.private_key_pem)
        return {"Authorization": f"DNSid {token}"}
