"""Serves /.well-known/jwks.json for guild subdomains.

Each guild gets one EC P-256 key pair, generated lazily on first request.
The public key is returned as a JWK so external consumers can verify
guild-issued tokens.
"""

from __future__ import annotations

import base64
import secrets
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from database import get_db
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from models import Guild, GuildKey
from sqlalchemy import select

router = APIRouter()


def _extract_guild_from_host(host: str) -> str | None:
    hostname = host.split(":")[0]
    for base in ("pioneer-square.melloy.life", "localhost"):
        suffix = f".{base}"
        if hostname.endswith(suffix):
            return hostname[: -len(suffix)]
    return None


def _public_key_to_jwk(guild_key: GuildKey) -> dict:
    pub = serialization.load_pem_public_key(guild_key.public_key_pem.encode())
    nums = pub.public_numbers()  # type: ignore[union-attr]

    def _b64url(n: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()

    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": guild_key.key_id,
        "use": "sig",
        "x": _b64url(nums.x),
        "y": _b64url(nums.y),
    }


async def _get_or_create_guild_key(guild_id: str) -> GuildKey | None:
    db = await get_db()
    try:
        row = (
            await db.execute(select(GuildKey).where(GuildKey.guild_id == guild_id))
        ).scalar_one_or_none()
        if row:
            return row

        guild = (await db.execute(select(Guild).where(Guild.id == guild_id))).scalar_one_or_none()
        if not guild:
            return None

        private_key = ec.generate_private_key(ec.SECP256R1())
        pub_pem = (
            private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        priv_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

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


@router.get("/.well-known/jwks.json")
async def jwks(request: Request) -> JSONResponse:
    guild_id = _extract_guild_from_host(request.headers.get("host", ""))
    if not guild_id:
        return JSONResponse({"keys": []})

    guild_key = await _get_or_create_guild_key(guild_id)
    if not guild_key:
        return JSONResponse({"keys": []})

    return JSONResponse({"keys": [_public_key_to_jwk(guild_key)]})
