"""Serves /.well-known/jwks.json and /.well-known/agent.json for guild
subdomains, and manages guild keys.

Each guild gets one Ed25519 key pair, generated lazily on first request.
The public key is returned as a JWK (OKP / Ed25519) with a kid equal to its
RFC 7638 thumbprint so external consumers can verify guild-issued tokens.

Custom keys can be uploaded via PUT /guilds/{guild_id}/jwks — when set they
replace the auto-generated key in the .well-known response. A private key JWK
can also be stored (never served) for backend signing.

The agent.json endpoint implements the A2A (Agent-to-Agent) protocol AgentCard
(https://google.github.io/A2A/specification/#agent-card), describing the guild's
foreman agent and its worker skills.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from models import Guild, GuildKey, Worker
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
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
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            return None

        row = (
            await db.execute(select(GuildKey).where(GuildKey.guild_id == guild_pk))
        ).scalar_one_or_none()

        if row:
            # Migrate any existing P-256 key to Ed25519.
            pub = serialization.load_pem_public_key(row.public_key_pem.encode())
            if not isinstance(pub, Ed25519PublicKey):
                row.public_key_pem, row.private_key_pem = _generate_ed25519_pems()
                await db.commit()
                await db.refresh(row)
            return row

        pub_pem, priv_pem = _generate_ed25519_pems()
        row = GuildKey(
            guild_id=guild_pk,
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
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        row = (
            await db.execute(select(GuildKey).where(GuildKey.guild_id == guild_pk))
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


# ---------------------------------------------------------------------------
# A2A AgentCard — /.well-known/agent.json
# ---------------------------------------------------------------------------


class _CamelModel(BaseModel):
    """Base model that serialises to camelCase JSON per the A2A spec."""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class AgentCapabilities(_CamelModel):
    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False


class AgentSkill(_CamelModel):
    id: str
    name: str
    description: str
    tags: list[str]
    examples: list[str] = []
    input_modes: list[str] = []
    output_modes: list[str] = []


class AgentProvider(_CamelModel):
    organization: str
    url: str | None = None


class AgentCard(_CamelModel):
    name: str
    description: str
    url: str
    version: str
    capabilities: AgentCapabilities
    default_input_modes: list[str]
    default_output_modes: list[str]
    skills: list[AgentSkill]
    provider: AgentProvider | None = None
    documentation_url: str | None = None
    # Security schemes advertised when the A2A receiver + OIDC are enabled.
    # Excluded from serialisation when None (exclude_none=True in model_dump).
    security_schemes: dict | None = None
    security: list[dict] | None = None


def _worker_to_skill(worker: Worker) -> AgentSkill:
    repos: list[str] = []
    try:
        repos = json.loads(worker.repos or "[]")
    except (ValueError, TypeError):
        pass

    if worker.org:
        name = f"Worker ({worker.org})"
        description = f"Handles coding tasks for GitHub org {worker.org}"
        tags = ["coding", "github", worker.org]
    elif repos:
        name = f"Worker ({', '.join(repos[:2])}{'…' if len(repos) > 2 else ''})"
        description = f"Handles coding tasks for: {', '.join(repos)}"
        tags = ["coding", "github"] + repos[:5]
    else:
        name = f"Worker ({worker.id})"
        description = "Handles coding tasks"
        tags = ["coding"]

    return AgentSkill(
        id=worker.id,
        name=name,
        description=description,
        tags=tags,
    )


def _oidc_security_schemes(base_url: str) -> tuple[dict | None, list[dict] | None]:
    """Return (securitySchemes, security) when OIDC is enabled, else (None, None)."""
    # Imported lazily to avoid triggering foreman/__init__ circular imports.
    from foreman.oidc import OIDCConfig, is_a2a_receiver_enabled

    if not is_a2a_receiver_enabled():
        return None, None
    config = OIDCConfig.from_env()
    issuer = config.issuer or base_url
    return (
        {
            "oidc": {
                "type": "openIdConnect",
                "openIdConnectUrl": f"{issuer.rstrip('/')}/.well-known/openid-configuration",
            }
        },
        [{"oidc": []}],
    )


def _build_agent_card(
    guild: Guild | None,
    workers: list[Worker],
    base_url: str,
) -> AgentCard:
    guild_name = (guild.name if guild and guild.name else "Pioneer Square") or "Pioneer Square"
    guild_description = (
        guild.description
        if guild and guild.description
        else (
            "A real-time multi-agent workspace where a Foreman AI coordinates "
            "worker agents that autonomously clone repos, run Claude on tasks, "
            "and open GitHub PRs."
        )
    )
    guild_url = (guild.url if guild and guild.url else base_url) or base_url
    guild_version = (guild.version if guild and guild.version else "1.0.0") or "1.0.0"

    online_workers = [w for w in workers if w.state not in ("offline",)]
    skills = [_worker_to_skill(w) for w in online_workers]

    # Always include the foreman as a skill
    foreman_skill = AgentSkill(
        id="foreman",
        name="Foreman",
        description=(
            "Orchestrates coding tasks: breaks down requests, assigns them to "
            "worker agents, reviews results, and requests follow-ups as needed."
        ),
        tags=["orchestration", "planning", "review"],
        examples=[
            "Add a dark-mode toggle to the settings page",
            "Fix the bug in the authentication flow",
            "Refactor the database layer to use async queries",
        ],
    )
    skills.insert(0, foreman_skill)

    security_schemes, security = _oidc_security_schemes(base_url)

    return AgentCard(
        name=guild_name,
        description=guild_description,
        url=guild_url,
        version=guild_version,
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=skills,
        provider=AgentProvider(
            organization="Pioneer Square",
            url="https://github.com/jmelloy/pioneer-square",
        ),
        documentation_url="https://github.com/jmelloy/pioneer-square#readme",
        security_schemes=security_schemes,
        security=security,
    )


@router.get("/.well-known/agent.json")
async def agent_card(request: Request) -> JSONResponse:
    guild_id = _extract_guild_from_host(request.headers.get("host", ""))

    guild: Guild | None = None
    workers: list[Worker] = []

    if guild_id:
        db = await get_db()
        try:
            guild = (
                await db.execute(select(Guild).where(Guild.guild_id == guild_id))
            ).scalar_one_or_none()
            if guild:
                rows = await db.execute(select(Worker).where(Worker.guild_id == guild.id))
                workers = list(rows.scalars().all())
        finally:
            await db.close()

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    base_url = f"{scheme}://{host}"

    card = _build_agent_card(guild, workers, base_url)
    return JSONResponse(
        content=card.model_dump(by_alias=True, exclude_none=True),
        headers={"Content-Type": "application/json"},
    )


@router.get("/guilds/{guild_id}/agent-card")
async def guild_agent_card(
    guild_id: str,
    request: Request,
    _: str = Depends(require_member("owner", "member", "viewer")),
) -> JSONResponse:
    """Returns the AgentCard for a specific guild (authenticated)."""
    db = await get_db()
    try:
        guild = (
            await db.execute(select(Guild).where(Guild.guild_id == guild_id))
        ).scalar_one_or_none()
        if not guild:
            raise HTTPException(404, detail="Guild not found")
        rows = await db.execute(select(Worker).where(Worker.guild_id == guild.id))
        workers = list(rows.scalars().all())
    finally:
        await db.close()

    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    base_url = f"{scheme}://{host}"

    card = _build_agent_card(guild, workers, base_url)
    return JSONResponse(
        content=card.model_dump(by_alias=True, exclude_none=True),
        headers={"Content-Type": "application/json"},
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
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(404, detail="Guild not found")

        row = (
            await db.execute(select(GuildKey).where(GuildKey.guild_id == guild_pk))
        ).scalar_one_or_none()

        if not row:
            pub_pem, priv_pem = _generate_ed25519_pems()
            row = GuildKey(
                guild_id=guild_pk,
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
