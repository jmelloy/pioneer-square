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
- ``require_any_worker_or_member_path`` — like ``require_worker_or_member_path``
  but does not require the token belong to the ``guild_id`` in the path; any
  authenticated worker/member/foreman token is accepted. Used for read-only
  task-state lookups (see #879) where guild membership isn't a meaningful
  restriction.
- ``require_debug_token`` — validates the ``DEBUG_TOKEN`` env var against an
  ``Authorization: Bearer`` or ``X-Debug-Token`` header. Used to gate the
  ``/debug/...`` routes, which are only registered when ``DEBUG_TOKEN`` is set.

Helper:

- ``get_guild_pk(db, guild_id)`` — look up the integer PK for a guild string
  identifier. Returns None when the guild does not exist.
"""

from __future__ import annotations

import os

from database import get_db
from fastapi import Depends, Header, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from models import Guild, GuildMember, UserSession, Worker
from sqlmodel import col, select

http_bearer = HTTPBearer(auto_error=False)


async def get_guild_pk(db, guild_id: str) -> int | None:
    """Return the integer PK (guilds.id) for *guild_id* (the slug), or None if not found."""
    result = await db.exec(select(col(Guild.id)).where(col(Guild.slug) == guild_id))
    return result.one_or_none()


async def require_user(
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
) -> str:
    """Validate the login_token and return ``github_user_id``."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    token = credentials.credentials
    db = await get_db()
    try:
        result = await db.exec(
            select(col(UserSession.github_user_id)).where(col(UserSession.token) == token)
        )
        github_user_id = result.one_or_none()
        if not github_user_id:
            raise HTTPException(status_code=401, detail="Invalid or expired session token")
        return github_user_id
    finally:
        await db.close()


async def ensure_membership(db, guild_id: str, user_id: str) -> str:
    """Return the role of *user_id* in *guild_id* or raise HTTP 403/404."""
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")

    res = await db.exec(
        select(col(GuildMember.role)).where(
            col(GuildMember.guild_id) == guild_pk, col(GuildMember.user_id) == user_id
        )
    )
    role = res.one_or_none()
    if role:
        return role
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


async def authorize_worker_or_member(guild_id: str, token: str | None) -> str:  # noqa: C901
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

    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")

        worker_res = await db.exec(
            select(col(Worker.id)).where(
                col(Worker.guild_id) == guild_pk, col(Worker.auth_token) == token
            )
        )
        worker_id = worker_res.one_or_none()
        if worker_id:
            return f"worker:{worker_id}"

        user_res = await db.exec(
            select(col(UserSession.github_user_id)).where(col(UserSession.token) == token)
        )
        github_user_id = user_res.one_or_none()
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


async def authorize_any_worker_or_member(token: str | None) -> str:
    """Validate *token* as a worker auth_token or member login_token from
    *any* guild — no guild-membership check.

    Used for read-only task-state lookups where the caller only needs to be
    a known worker or member somewhere, not specifically of the guild being
    queried (see #879: the tasks-table read endpoints intentionally dropped
    the guild-only restriction). Returns ``"worker:<worker_id>"`` or
    ``"user:<github_user_id>"``. Raises 401 on an unrecognised token.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    db = await get_db()
    try:
        worker_res = await db.exec(select(col(Worker.id)).where(col(Worker.auth_token) == token))
        worker_id = worker_res.one_or_none()
        if worker_id:
            return f"worker:{worker_id}"

        user_res = await db.exec(
            select(col(UserSession.github_user_id)).where(col(UserSession.token) == token)
        )
        github_user_id = user_res.one_or_none()
        if github_user_id:
            return f"user:{github_user_id}"

        raise HTTPException(status_code=401, detail="Invalid credentials")
    finally:
        await db.close()


async def require_any_worker_or_member_path(
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
) -> str:
    """Dependency for read-only task-state endpoints: any valid worker or
    member token authenticates the caller, without requiring the token be
    scoped to the ``guild_id`` in the path."""
    token = credentials.credentials if credentials else None
    return await authorize_any_worker_or_member(token)


def require_debug_token(
    authorization: str | None = Header(default=None),
    x_debug_token: str | None = Header(default=None),
) -> None:
    """Validate the caller against the ``DEBUG_TOKEN`` env var.

    Accepts either ``Authorization: Bearer <token>`` or ``X-Debug-Token:
    <token>``. Raises 401 if no token was supplied, 403 if it doesn't match.
    Routes depending on this should only be registered when ``DEBUG_TOKEN``
    is set — see ``routes/debug_query.py`` and its conditional mount in
    ``main.py``.
    """
    expected = os.environ.get("DEBUG_TOKEN", "")
    token = x_debug_token
    if not token and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            token = value
    if not token:
        raise HTTPException(status_code=401, detail="Debug token required")
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="Invalid debug token")
