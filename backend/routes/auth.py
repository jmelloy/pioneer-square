"""Authentication and identity routes.

Covers GitHub OAuth (login/exchange/callback/token), Claude credential
storage, the ``/auth/me`` + ``/api/me`` profile endpoints, and logout.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime

from auth_deps import (
    authorize_worker_or_member,
    get_guild_pk,
    http_bearer,
    require_user,
    require_worker_or_member,
)
from database import get_db
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from models import (
    ClaudeCredentials,
    GithubToken,
    Guild,
    GuildMember,
    User,
    UserSession,
)
from oauth import FRONTEND_URL, GITHUB_CLIENT_ID, create_session, get_return_to, make_authorize_url
from pydantic import BaseModel
from sqlalchemy import delete, select

router = APIRouter()


class CodeExchangeRequest(BaseModel):
    code: str
    state: str


class ClaudeCredentialsRequest(BaseModel):
    guild_id: str
    credentials_blob: str  # base64-encoded tar.gz of ~/.claude/


@router.get("/auth/github/login")
async def github_login(return_to: str | None = Query(None)):
    """Start the GitHub OAuth flow. Returns the authorization URL.

    ``return_to`` is an optional origin URL (e.g. a guild subdomain) to redirect
    back to after the OAuth callback, instead of the default FRONTEND_URL.
    """
    if not GITHUB_CLIENT_ID:
        raise HTTPException(
            status_code=500, detail="GitHub OAuth not configured (missing GITHUB_CLIENT_ID)"
        )
    return {"url": make_authorize_url(return_to)}


@router.post("/auth/github/exchange")
async def github_exchange(body: CodeExchangeRequest):
    """Exchange a GitHub OAuth code+state for a login session. Called by the frontend."""
    return await create_session(body.code, body.state)


@router.get("/auth/github/callback")
async def github_callback(code: str = Query(...), state: str = Query(...)):
    """Legacy backend OAuth callback — redirects to the frontend with session params in the query string."""
    return_to = get_return_to(state)  # peek before create_session pops the state
    payload = await create_session(code, state)
    qs = urllib.parse.urlencode(payload)
    base = (return_to or FRONTEND_URL).rstrip("/")
    return RedirectResponse(url=f"{base}/?{qs}")


@router.get("/auth/github/token")
async def get_github_token(
    guild_id: str = Query(...),
    _principal: str = Depends(require_worker_or_member),
):
    """Return the stored OAuth token for the guild's linked GitHub user. Used by workers.

    Requires a worker auth_token (from registration) or a member login_token.
    Without auth, anyone knowing a guild_id could exfiltrate the GitHub token."""
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="No GitHub account linked to this guild")
        owner_res = await db.execute(
            select(GuildMember.user_id)
            .where(GuildMember.guild_pk == guild_pk, GuildMember.role == "owner")
            .limit(1)
        )
        owner_user_id = owner_res.scalar_one_or_none()
        if not owner_user_id:
            raise HTTPException(status_code=404, detail="No GitHub account linked to this guild")
        result = await db.execute(
            select(GithubToken.access_token, GithubToken.github_username).where(
                GithubToken.github_user_id == owner_user_id
            )
        )
        token_row = result.first()
        if not token_row:
            raise HTTPException(status_code=404, detail="GitHub token not found")
        return {"access_token": token_row.access_token, "username": token_row.github_username}
    finally:
        await db.close()


@router.get("/auth/claude/credentials")
async def get_claude_credentials(
    guild_id: str = Query(...),
    _principal: str = Depends(require_worker_or_member),
):
    """Return stored Claude credentials blob for a worker. Called by workers on startup.

    Requires a worker auth_token (from registration) or a member login_token —
    these credentials are sensitive and must not be readable by guild_id alone."""
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, guild_id)
        if guild_pk is None:
            raise HTTPException(
                status_code=404, detail="No Claude credentials stored for this guild"
            )
        result = await db.execute(
            select(ClaudeCredentials).where(ClaudeCredentials.guild_pk == guild_pk)
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(
                status_code=404, detail="No Claude credentials stored for this guild"
            )
        return {"credentials_blob": row.credentials_blob}
    finally:
        await db.close()


@router.post("/auth/claude/credentials")
async def store_claude_credentials(
    data: ClaudeCredentialsRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
):
    """Store Claude credentials blob (called by worker after successful login).

    Requires a worker auth_token or member login_token for ``data.guild_id``.
    Without this check, anyone could overwrite a guild's Claude credentials."""
    token = credentials.credentials if credentials else None
    await authorize_worker_or_member(data.guild_id, token)
    now = datetime.now(UTC).isoformat()
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, data.guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.execute(
            select(ClaudeCredentials).where(ClaudeCredentials.guild_pk == guild_pk)
        )
        row = result.scalar_one_or_none()
        if row:
            row.credentials_blob = data.credentials_blob
            row.updated_at = now
        else:
            db.add(
                ClaudeCredentials(
                    guild_pk=guild_pk,
                    credentials_blob=data.credentials_blob,
                    updated_at=now,
                )
            )
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


@router.get("/auth/me")
async def get_me(github_user_id: str = Depends(require_user)):
    """Return the currently authenticated user's info."""
    db = await get_db()
    try:
        result = await db.execute(
            select(
                GithubToken.github_user_id,
                GithubToken.github_username,
                GithubToken.scope,
            ).where(GithubToken.github_user_id == github_user_id)
        )
        row = result.first()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "github_user_id": row.github_user_id,
            "github_username": row.github_username,
            "scope": row.scope,
        }
    finally:
        await db.close()


@router.get("/api/me")
async def api_me(github_user_id: str = Depends(require_user)):
    """Return the current user's profile + their guild memberships."""
    db = await get_db()
    try:
        u_res = await db.execute(select(User).where(User.id == github_user_id))
        user = u_res.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        members_res = await db.execute(
            select(Guild.guild_id, GuildMember.role, Guild.name)
            .join(Guild, Guild.id == GuildMember.guild_pk)
            .where(GuildMember.user_id == github_user_id)
        )
        memberships = [
            {"guild_id": row.guild_id, "guild_name": row.name, "role": row.role}
            for row in members_res.fetchall()
        ]
        return {
            "user": {
                "id": user.id,
                "github_id": user.github_id,
                "github_login": user.github_login,
                "email": user.email,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
            },
            "memberships": memberships,
        }
    finally:
        await db.close()


@router.delete("/auth/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(http_bearer)):
    """Invalidate the current login_token."""
    if credentials:
        db = await get_db()
        try:
            await db.execute(
                delete(UserSession).where(UserSession.token == credentials.credentials)
            )
            await db.commit()
        finally:
            await db.close()
    return {"status": "logged_out"}
