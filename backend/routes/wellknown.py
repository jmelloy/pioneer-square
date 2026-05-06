"""Serves /.well-known/jwks.json for guild subdomains and manages guild keys.

Each guild gets one EC P-256 key pair, generated lazily on first request.
The public key is returned as a JWK so external consumers can verify
guild-issued tokens.

Custom keys can be uploaded via PUT /guilds/{guild_id}/jwks — when set they
replace the auto-generated key in the .well-known response. A private key JWK
can also be stored (never served) for backend signing.
"""

from __future__ import annotations

import base64
import json
import secrets
from datetime import UTC, datetime

from auth_deps import require_member
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
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
            # Ensure guild exists before creating a key row
            guild = (
                await db.execute(select(Guild).where(Guild.id == guild_id))
            ).scalar_one_or_none()
            if not guild:
                raise HTTPException(404, detail="Guild not found")

            # Placeholder auto-generated key so NOT NULL columns are satisfied
            private_key = ec.generate_private_key(ec.SECP256R1())
            row = GuildKey(
                guild_id=guild_id,
                key_id=secrets.token_urlsafe(16),
                public_key_pem=private_key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
                .decode(),
                private_key_pem=private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
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
