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
"""

from __future__ import annotations

from database import get_db
from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from models import Guild, GuildMember, UserSession, Worker
from sqlalchemy import select

http_bearer = HTTPBearer(auto_error=False)


async def require_user(
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> str:
    """Validate the login_token and return ``github_user_id``."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    token = credentials.credentials
    db = await get_db()
    try:
        result = await db.execute(
            select(UserSession.github_user_id).where(UserSession.token == token)
        )
        github_user_id = result.scalar_one_or_none()
        if not github_user_id:
            raise HTTPException(status_code=401, detail="Invalid or expired session token")
        return github_user_id
    finally:
        await db.close()


async def ensure_membership(db, guild_id: str, user_id: str) -> str:
    """Return the role of *user_id* in *guild_id* or raise HTTP 403/404.

    Legacy guilds created before guild_members existed have a ``github_user_id``
    on the guild row and no member rows; treat that owner as a member so the
    pre-migration UI keeps working without a manual repair step.
    """
    g_res = await db.execute(
        select(Guild.guild_id, Guild.github_user_id).where(Guild.guild_id == guild_id)
    )
    grow = g_res.first()
    if not grow:
        raise HTTPException(status_code=404, detail="Guild not found")

    res = await db.execute(
        select(GuildMember.role).where(
            GuildMember.guild_id == guild_id, GuildMember.user_id == user_id
        )
    )
    role = res.scalar_one_or_none()
    if role:
        return role
    if grow.github_user_id and grow.github_user_id == user_id:
        return "owner"
    raise HTTPException(status_code=403, detail="Not a member of this guild")


def require_member(*allowed_roles: str):
    """Dependency factory: caller must be a member, optionally with one of
    the listed roles."""

    async def _dep(guild_id: str, github_user_id: str = Depends(require_user)) -> str:
        db = await get_db()
        try:
            role = await ensure_membership(db, guild_id, github_user_id)
        finally:
            await db.close()
        if allowed_roles and role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"This action requires one of: {', '.join(allowed_roles)}",
            )
        return github_user_id

    return _dep


async def authorize_worker_or_member(guild_id: str, token: str | None) -> str:
    """Validate *token* against worker auth_tokens or member login_tokens.

    Returns ``"worker:<worker_id>"`` or ``"user:<github_user_id>"`` so callers
    can audit-log the principal. Raises 401/403/404 (matching require_member's
    contract) on failure.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    db = await get_db()
    try:
        worker_res = await db.execute(
            select(Worker.id).where(Worker.guild_id == guild_id, Worker.auth_token == token)
        )
        worker_id = worker_res.scalar_one_or_none()
        if worker_id:
            return f"worker:{worker_id}"

        user_res = await db.execute(
            select(UserSession.github_user_id).where(UserSession.token == token)
        )
        github_user_id = user_res.scalar_one_or_none()
        if not github_user_id:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        await ensure_membership(db, guild_id, github_user_id)
        return f"user:{github_user_id}"
    finally:
        await db.close()


async def require_worker_or_member(
    guild_id: str = Query(...),
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
) -> str:
    """Dependency for query-string endpoints that fetch guild secrets.

    Accepts either a worker auth_token (issued at registration) or a member
    login_token. Returns the principal string; see ``authorize_worker_or_member``.
    """
    token = credentials.credentials if credentials else None
    return await authorize_worker_or_member(guild_id, token)


async def require_worker_or_member_path(
    guild_id: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
) -> str:
    """Dependency for path-string endpoints (``/guilds/{guild_id}/...``).

    Same contract as ``require_worker_or_member`` but reads ``guild_id`` from
    the path instead of the query string. Useful when the canonical URL
    already carries the guild id and adding a duplicate query param would be
    redundant.
    """
    token = credentials.credentials if credentials else None
    return await authorize_worker_or_member(guild_id, token)
