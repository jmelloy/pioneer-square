"""Serves /.well-known/jwks.json for guild subdomains and manages guild keys.

Each guild gets one Ed25519 key pair, generated lazily on first request.
The public key is returned as a JWK (OKP / Ed25519) with a kid equal to its
RFC 7638 thumbprint so external consumers can verify guild-issued tokens.

Custom keys can be uploaded via PUT /guilds/{guild_id}/jwks — when set they
replace the auto-generated key in the .well-known response. A private key JWK
can also be stored (never served) for backend signing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime

from auth_deps import require_member
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from models import Guild, GuildKey
from pydantic import BaseModel
from sqlalchemy import select

router = APIRouter()


def _extract_guild_from_host(host: str) -> str | None:
    hostname = host.split(":")[0]
    for base in ("pioneer-square.melloy.life", "localhost"):
        suffix = f".{base}"
        if hostname.endswith(suffix):
            return hostname[: -len(suffix)]
    return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _public_key_to_jwk(guild_key: GuildKey) -> dict:
    pub = serialization.load_pem_public_key(guild_key.public_key_pem.encode())
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    x = _b64url(raw)
    # RFC 7638: SHA-256 of canonical JSON of required members in lex order.
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True
    )
    kid = _b64url(hashlib.sha256(canonical.encode()).digest())
    return {"kty": "OKP", "crv": "Ed25519", "x": x, "kid": kid, "use": "sig"}


def _generate_ed25519_pems() -> tuple[str, str]:
    """Return (public_pem, private_pem) for a fresh Ed25519 key pair."""
    priv = Ed25519PrivateKey.generate()
    pub_pem = (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pub_pem, priv_pem


async def _get_or_create_guild_key(guild_id: str) -> GuildKey | None:
    db = await get_db()
    try:
        row = (
            await db.execute(select(GuildKey).where(GuildKey.guild_id == guild_id))
        ).scalar_one_or_none()

        if row:
            # Migrate any existing P-256 key to Ed25519.
            pub = serialization.load_pem_public_key(row.public_key_pem.encode())
            if not isinstance(pub, Ed25519PublicKey):
                row.public_key_pem, row.private_key_pem = _generate_ed25519_pems()
                await db.commit()
                await db.refresh(row)
            return row

        guild = (await db.execute(select(Guild).where(Guild.id == guild_id))).scalar_one_or_none()
        if not guild:
            return None

        pub_pem, priv_pem = _generate_ed25519_pems()
        row = GuildKey(
            guild_id=guild_id,
            key_id=secrets.token_urlsafe(16),
            public_key_pem=pub_pem,
            private_key_pem=priv_pem,
            created_at=datetime.now(UTC).isoformat(),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Public .well-known endpoint
# ---------------------------------------------------------------------------


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> JSONResponse:
    guild_id = _extract_guild_from_host(request.headers.get("host", ""))
    if not guild_id:
        return JSONResponse({"keys": []})

    guild_key = await _get_or_create_guild_key(guild_id)
    if not guild_key:
        return JSONResponse({"keys": []})

    if guild_key.custom_jwks:
        return JSONResponse(json.loads(guild_key.custom_jwks))

    return JSONResponse({"keys": [_public_key_to_jwk(guild_key)]})


# ---------------------------------------------------------------------------
# Guild key management
# ---------------------------------------------------------------------------


class JWKSConfig(BaseModel):
    public_jwks: dict  # {"keys": [...]}
    private_key_jwk: dict | None = None  # stored for signing, never served


@router.get("/guilds/{guild_id}/jwks")
async def get_jwks_config(
    guild_id: str,
    _: str = Depends(require_member("owner", "member")),
) -> JSONResponse:
    db = await get_db()
    try:
        row = (
            await db.execute(select(GuildKey).where(GuildKey.guild_id == guild_id))
        ).scalar_one_or_none()
    finally:
        await db.close()

    if not row or not row.custom_jwks:
        return JSONResponse({"custom_jwks": None, "has_private_key": False})

    return JSONResponse(
        {
            "custom_jwks": json.loads(row.custom_jwks),
            "has_private_key": bool(row.private_key_jwk),
        }
    )


@router.put("/guilds/{guild_id}/jwks")
async def set_jwks_config(
    guild_id: str,
    body: JWKSConfig,
    _: str = Depends(require_member("owner")),
) -> JSONResponse:
    if "keys" not in body.public_jwks or not isinstance(body.public_jwks["keys"], list):
        raise HTTPException(400, detail="public_jwks must have a 'keys' array")

    db = await get_db()
    try:
        row = (
            await db.execute(select(GuildKey).where(GuildKey.guild_id == guild_id))
        ).scalar_one_or_none()

        if not row:
            guild = (
                await db.execute(select(Guild).where(Guild.id == guild_id))
            ).scalar_one_or_none()
            if not guild:
                raise HTTPException(404, detail="Guild not found")

            pub_pem, priv_pem = _generate_ed25519_pems()
            row = GuildKey(
                guild_id=guild_id,
                key_id=secrets.token_urlsafe(16),
                public_key_pem=pub_pem,
                private_key_pem=priv_pem,
                created_at=datetime.now(UTC).isoformat(),
            )
            db.add(row)

        row.custom_jwks = json.dumps(body.public_jwks)
        if body.private_key_jwk is not None:
            row.private_key_jwk = json.dumps(body.private_key_jwk)

        await db.commit()
    finally:
        await db.close()

    return JSONResponse({"ok": True})
