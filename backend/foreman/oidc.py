"""OIDC (OpenID Connect) authentication support for the A2A receiver.

When the A2A receiver is enabled (``A2A_RECEIVER_ENABLED=true``), worker
agents may authenticate using OIDC JWT tokens instead of the opaque bearer
tokens issued at registration.  The backend validates the token against the
issuer's JWKS endpoint and checks standard claims (iss, aud, exp).

Environment variables
---------------------
A2A_RECEIVER_ENABLED
    Set to ``true`` / ``1`` / ``yes`` to enable the A2A receiver and OIDC
    authentication.  When unset or falsy, OIDC tokens are rejected and the
    AgentCard advertises no OIDC security scheme.

OIDC_ISSUER
    The expected OIDC issuer URL (e.g. ``https://accounts.example.com``).
    The JWKS is fetched from ``{OIDC_ISSUER}/.well-known/jwks.json``.
    If unset, any issuer in the token is accepted and its JWKS is used
    for signature verification (useful for guild-issued self-signed tokens).

OIDC_AUDIENCE
    Expected ``aud`` claim value.  Defaults to ``"pioneer-square"``.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def is_a2a_receiver_enabled() -> bool:
    """Return True when the A2A receiver is configured via environment."""
    return os.environ.get("A2A_RECEIVER_ENABLED", "").lower() in ("1", "true", "yes")


@dataclass
class OIDCConfig:
    """OIDC validation configuration derived from environment variables."""

    issuer: str | None
    audience: str

    @classmethod
    def from_env(cls) -> OIDCConfig:
        return cls(
            issuer=os.environ.get("OIDC_ISSUER") or None,
            audience=os.environ.get("OIDC_AUDIENCE", "pioneer-square"),
        )


# ---------------------------------------------------------------------------
# JWT utilities (no external JWT library — uses only stdlib + cryptography)
# ---------------------------------------------------------------------------


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _decode_jwt_parts(token: str) -> tuple[dict, dict, bytes, bytes]:
    """Split *token* into (header, payload, signing_input_bytes, signature_bytes).

    Does NOT verify the signature — call :func:`validate_oidc_token` for that.
    Raises :class:`ValueError` on malformed tokens.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Not a valid JWT (expected 3 dot-separated parts)")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception as exc:
        raise ValueError(f"Could not decode JWT header/payload: {exc}") from exc
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    try:
        signature = _b64url_decode(parts[2])
    except Exception as exc:
        raise ValueError(f"Could not decode JWT signature: {exc}") from exc
    return header, payload, signing_input, signature


def _fetch_jwks(jwks_url: str) -> list[dict]:
    """Fetch a JWKS document from *jwks_url* (synchronous HTTP GET)."""
    req = urllib.request.Request(jwks_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data.get("keys", [])


def _find_jwk(keys: list[dict], kid: str | None) -> dict | None:
    """Return the JWK matching *kid*, or the first key when *kid* is None."""
    if kid:
        for key in keys:
            if key.get("kid") == kid:
                return key
    return keys[0] if keys else None


def _verify_ed25519(jwk: dict, signing_input: bytes, signature: bytes) -> None:
    """Verify an EdDSA/Ed25519 signature against a JWK.  Raises on failure."""
    try:
        x_bytes = _b64url_decode(jwk["x"])
    except Exception as exc:
        raise ValueError(f"Invalid JWK 'x' field: {exc}") from exc
    pub = Ed25519PublicKey.from_public_bytes(x_bytes)
    try:
        pub.verify(signature, signing_input)
    except InvalidSignature as exc:
        raise ValueError("Token signature is invalid") from exc


# ---------------------------------------------------------------------------
# Public validation entry point
# ---------------------------------------------------------------------------


def validate_oidc_token(token: str, config: OIDCConfig) -> dict:
    """Validate an OIDC JWT and return the verified payload claims.

    Checks performed:
    - JWT is structurally valid (3 parts, valid base64url/JSON).
    - ``exp`` claim has not passed (when present).
    - ``iss`` matches ``config.issuer`` when that is set.
    - Signature verified against the issuer's JWKS (Ed25519 / EdDSA only).
    - ``aud`` contains ``config.audience`` (when present in token).

    Raises :class:`ValueError` with a human-readable message on any failure.
    Returns the payload dict on success.
    """
    header, payload, signing_input, signature = _decode_jwt_parts(token)

    # Expiry check
    exp = payload.get("exp")
    if exp is not None and int(time.time()) > int(exp):
        raise ValueError("OIDC token has expired")

    # Issuer check
    iss = payload.get("iss")
    if not iss:
        raise ValueError("OIDC token is missing the 'iss' claim")
    if config.issuer and iss != config.issuer:
        raise ValueError(f"Unexpected issuer {iss!r} (expected {config.issuer!r})")

    # Fetch JWKS and verify signature
    jwks_url = f"{iss.rstrip('/')}/.well-known/jwks.json"
    try:
        keys = _fetch_jwks(jwks_url)
    except Exception as exc:
        raise ValueError(f"Could not fetch JWKS from {jwks_url}: {exc}") from exc

    kid = header.get("kid")
    jwk = _find_jwk(keys, kid)
    if not jwk:
        raise ValueError(f"No suitable key found in JWKS (kid={kid!r})")

    alg = header.get("alg", "")
    if alg in ("EdDSA", "Ed25519"):
        _verify_ed25519(jwk, signing_input, signature)
    else:
        raise ValueError(f"Unsupported signing algorithm {alg!r} — only EdDSA/Ed25519 is supported")

    # Audience check
    aud = payload.get("aud")
    if aud is not None:
        if isinstance(aud, str):
            aud = [aud]
        if config.audience not in aud:
            raise ValueError(f"Audience {config.audience!r} not found in token aud {aud!r}")

    return payload
