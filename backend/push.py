"""APNs dispatch for registered push tokens.

``send_to_user(user_id, ...)`` looks up every token registered by that user
and sends a notification to each via Apple's HTTP/2 endpoint. When APNs
credentials aren't configured (env vars unset) the call logs and is a
no-op, so the rest of the backend can call into this module unconditionally.

Env vars (all four required to actually send):

- ``APNS_KEY_P8``   — contents of the Apple .p8 private key (PEM)
- ``APNS_KEY_ID``   — 10-character Apple key id
- ``APNS_TEAM_ID``  — 10-character Apple team id
- ``APNS_BUNDLE_ID``— app bundle id used as the ``apns-topic`` header
- ``APNS_USE_SANDBOX`` — set to ``1`` to hit ``api.sandbox.push.apple.com``
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from database import get_db
from models import PushToken
from sqlalchemy import delete, select

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _APNsConfig:
    key_pem: str
    key_id: str
    team_id: str
    bundle_id: str
    sandbox: bool

    @property
    def host(self) -> str:
        return (
            "https://api.sandbox.push.apple.com" if self.sandbox else "https://api.push.apple.com"
        )


def _load_config() -> _APNsConfig | None:
    key = os.environ.get("APNS_KEY_P8", "").strip()
    key_id = os.environ.get("APNS_KEY_ID", "").strip()
    team_id = os.environ.get("APNS_TEAM_ID", "").strip()
    bundle = os.environ.get("APNS_BUNDLE_ID", "").strip()
    if not (key and key_id and team_id and bundle):
        return None
    return _APNsConfig(
        key_pem=key,
        key_id=key_id,
        team_id=team_id,
        bundle_id=bundle,
        sandbox=os.environ.get("APNS_USE_SANDBOX") == "1",
    )


# In-memory JWT cache. Apple requires ES256 tokens, accepts them for up to
# 60 minutes, and rate-limits regeneration — we cap to 45 minutes for a safe
# margin and regenerate on demand.
_JWT_TTL_SECONDS = 45 * 60
_jwt_cache: dict[str, tuple[str, float]] = {}


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_jwt(config: _APNsConfig) -> str:
    cached = _jwt_cache.get(config.key_id)
    now = time.time()
    if cached is not None:
        token, issued_at = cached
        if now - issued_at < _JWT_TTL_SECONDS:
            return token

    header = {"alg": "ES256", "kid": config.key_id, "typ": "JWT"}
    payload = {"iss": config.team_id, "iat": int(now)}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(config.key_pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("APNS_KEY_P8 must be an EC P-256 key")

    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    # APNs requires the raw (r||s) signature, not the DER-encoded form
    # cryptography returns by default.
    r, s = decode_dss_signature(der)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    jwt = signing_input + "." + _b64url(raw_sig)
    _jwt_cache[config.key_id] = (jwt, now)
    return jwt


async def _delete_tokens(tokens: list[str]) -> None:
    if not tokens:
        return
    db = await get_db()
    try:
        await db.exec(delete(PushToken).where(PushToken.token.in_(tokens)))
        await db.commit()
    finally:
        await db.close()


async def send_to_user(
    user_id: str,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Send a push to every device registered by ``user_id``.

    Returns the count of pushes Apple accepted (``200 OK``). When APNs creds
    aren't configured, logs the intended push and returns ``0``. ``410 Gone``
    tokens are removed from the database.
    """
    config = _load_config()

    db = await get_db()
    try:
        result = await db.exec(select(PushToken.token).where(PushToken.user_id == user_id))
        tokens = [row[0] for row in result.all()]
    finally:
        await db.close()

    if not tokens:
        return 0

    if config is None:
        logger.info(
            "push: APNs not configured; would have sent to %d device(s) for user %s",
            len(tokens),
            user_id,
        )
        return 0

    jwt = _make_jwt(config)
    apns_payload: dict[str, Any] = {
        "aps": {"alert": {"title": title, "body": body}, "sound": "default"}
    }
    if data:
        # Custom keys live alongside ``aps`` (Apple convention).
        apns_payload.update({k: v for k, v in data.items() if k != "aps"})
    body_json = json.dumps(apns_payload, separators=(",", ":"))

    delivered = 0
    stale: list[str] = []
    # http2=True requires the optional `h2` package; fall back gracefully.
    client_kwargs: dict[str, Any] = {"timeout": 5.0}
    try:
        import h2  # noqa: F401

        client_kwargs["http2"] = True
    except ImportError:
        logger.warning("push: h2 not installed; APNs will fail until 'httpx[http2]' is added")

    async with httpx.AsyncClient(**client_kwargs) as client:
        for token in tokens:
            try:
                resp = await client.post(
                    f"{config.host}/3/device/{token}",
                    headers={
                        "authorization": f"bearer {jwt}",
                        "apns-topic": config.bundle_id,
                        "apns-push-type": "alert",
                        "content-type": "application/json",
                    },
                    content=body_json,
                )
            except httpx.HTTPError as exc:
                logger.warning("push: APNs request failed for %s: %s", token[-8:], exc)
                continue
            if resp.status_code == 200:
                delivered += 1
            elif resp.status_code == 410:
                stale.append(token)
            else:
                logger.warning(
                    "push: APNs %s for %s: %s",
                    resp.status_code,
                    token[-8:],
                    resp.text[:200],
                )

    await _delete_tokens(stale)
    return delivered
