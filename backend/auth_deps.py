"""FastAPI dependencies for authenticating requests.

The dependencies here resolve a Bearer token (from the ``Authorization``
header) to a GitHub user id and, for guild-scoped routes, also enforce
membership and role checks.

Three flavours:

- ``require_user`` — any authenticated user.
- ``require_member(*allowed_roles)`` — caller must be a member of the
  ``guild_id`` path parameter, optionally with one of the listed roles.
- ``require_worker_or_member`` — accepts either a worker auth_token (issued
  at registration) or a member login_token; used by query-string endpoints
  that fetch guild secrets so workers can self-serve their credentials.

Helper:

- ``get_guild_pk(db, guild_id)`` — look up the integer PK for a guild string
  identifier. Returns None when the guild does not exist.
"""

from __future__ import annotations

from database import get_db
from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from models import Guild, GuildMember, UserSession, Worker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

http_bearer = HTTPBearer(auto_error=False)


async def get_guild_pk(db, guild_id: str) -> int | None:
    """Return the integer PK (guilds.id) for *guild_id*, or None if not found."""
    result = await db.execute(select(Guild.id).where(Guild.guild_id == guild_id))
    return result.scalar_one_or_none()


async def require_user(
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Validate the login_token and return ``github_user_id``."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    token = credentials.credentials
    result = await db.execute(select(UserSession.github_user_id).where(UserSession.token == token))
    github_user_id = result.scalar_one_or_none()
    if not github_user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session token")
    return github_user_id


async def ensure_membership(db, guild_id: str, user_id: str) -> str:
    """Return the role of *user_id* in *guild_id* or raise HTTP 403/404."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    res = await db.execute(
        select(GuildMember.role).where(
            GuildMember.guild_id == guild_pk, GuildMember.user_id == user_id
        )
    )
    role = res.scalar_one_or_none()
    if role:
        return role
    raise HTTPException(status_code=403, detail="Not a member of this guild")


def require_member(*allowed_roles: str):
    """Dependency factory: caller must be a member, optionally with one of
    the listed roles."""

    async def _dep(
        guild_id: str,
        github_user_id: str = Depends(require_user),
        db: AsyncSession = Depends(get_db),
    ) -> str:
        role = await ensure_membership(db, guild_id, github_user_id)
        if allowed_roles and role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"This action requires one of: {', '.join(allowed_roles)}",
            )
        return github_user_id

    return _dep


async def authorize_worker_or_member(guild_id: str, token: str | None, db: AsyncSession) -> str:  # noqa: C901
    """Validate *token* against worker auth_tokens or member login_tokens.

    Returns ``"worker:<worker_id>"`` or ``"user:<github_user_id>"`` so callers
    can audit-log the principal. Raises 401/403/404 (matching require_member's
    contract) on failure.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Foreman JWT — validated without a DB round-trip when PIONEER_FOREMAN_KEY is set.
    # Checked first so the foreman never waits for a DB session on the hot path.
    import os

    from utils import verify_foreman_jwt

    _foreman_key = os.environ.get("PIONEER_FOREMAN_KEY", "")
    if _foreman_key and verify_foreman_jwt(token, _foreman_key, guild_id):
        return "foreman:jwt"

    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    worker_res = await db.execute(
        select(Worker.id).where(Worker.guild_id == guild_pk, Worker.auth_token == token)
    )
    worker_id = worker_res.scalar_one_or_none()
    if worker_id:
        return f"worker:{worker_id}"

    user_res = await db.execute(
        select(UserSession.github_user_id).where(UserSession.token == token)
    )
    github_user_id = user_res.scalar_one_or_none()
    if github_user_id:
        await ensure_membership(db, guild_id, github_user_id)
        return f"user:{github_user_id}"

    # OIDC token fallback — only when the A2A receiver is enabled.
    # Imported lazily to avoid a circular import (foreman/__init__ → runner → auth_deps).
    from foreman.oidc import OIDCConfig, is_a2a_receiver_enabled, validate_oidc_token

    if is_a2a_receiver_enabled():
        try:
            config = OIDCConfig.from_env()
            payload = validate_oidc_token(token, config)
            sub = payload.get("sub") or "oidc-agent"
            return f"oidc:{sub}"
        except ValueError:
            pass

    raise HTTPException(status_code=401, detail="Invalid credentials")


async def require_worker_or_member(
    guild_id: str = Query(...),
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Dependency for query-string endpoints that fetch guild secrets.

    Accepts either a worker auth_token (issued at registration) or a member
    login_token. Returns the principal string; see ``authorize_worker_or_member``.
    """
    token = credentials.credentials if credentials else None
    return await authorize_worker_or_member(guild_id, token, db)


async def require_worker_or_member_path(
    guild_id: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Dependency for path-string endpoints (``/guilds/{guild_id}/...``).

    Same contract as ``require_worker_or_member`` but reads ``guild_id`` from
    the path instead of the query string. Useful when the canonical URL
    already carries the guild id and adding a duplicate query param would be
    redundant.
    """
    token = credentials.credentials if credentials else None
    return await authorize_worker_or_member(guild_id, token, db)
