"""HS256 JWT helpers for authenticating the standalone foreman with the backend.

Both sides share a secret (``backend_key`` in pioneer-foreman.toml /
``PIONEER_FOREMAN_KEY`` env var on the backend).  The foreman mints a
short-lived token at startup and refreshes it before expiry so REST calls
never fail due to an expired credential.

This module intentionally has no external dependencies — only stdlib.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

_FOREMAN_JWT_SUB = "pioneer-foreman"
_DEFAULT_TTL = 3600  # 1 hour
_REFRESH_BUFFER = 300  # refresh 5 minutes before expiry


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(guild_id: str, secret: str, ttl: int = _DEFAULT_TTL) -> str:
    """Mint a short-lived HS256 JWT for *guild_id* signed with *secret*."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url_encode(
        json.dumps(
            {
                "sub": _FOREMAN_JWT_SUB,
                "guild_id": guild_id,
                "iat": now,
                "exp": now + ttl,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    sig = _b64url_encode(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())
    return f"{signing_input}.{sig}"


def _exp_from_jwt(token: str) -> int:
    """Extract the ``exp`` claim from a JWT without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return 0
        padding = 4 - len(parts[1]) % 4
        if padding != 4:
            parts[1] += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(parts[1]))
        return int(payload.get("exp", 0))
    except Exception:
        return 0


class JWTTokenManager:
    """Holds a cached JWT and refreshes it when close to expiry.

    Usage::

        mgr = JWTTokenManager(guild_id="abc123", secret="s3cr3t")
        token = mgr.token()   # always returns a valid (non-expired) token

    Thread/async safety: token generation is pure CPU work; a refresh race
    between two concurrent coroutines at most mints two tokens, which is fine.
    """

    def __init__(self, guild_id: str, secret: str, ttl: int = _DEFAULT_TTL) -> None:
        self._guild_id = guild_id
        self._secret = secret
        self._ttl = ttl
        self._cached: str | None = None
        self._exp: int = 0

    def token(self) -> str:
        """Return a valid JWT, minting a fresh one if the current one is near expiry."""
        if not self._cached or time.time() >= self._exp - _REFRESH_BUFFER:
            self._cached = make_jwt(self._guild_id, self._secret, self._ttl)
            self._exp = _exp_from_jwt(self._cached)
        return self._cached
