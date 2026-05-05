"""Authentication and identity routes.

Covers GitHub OAuth (login/exchange/callback/token), Claude credential
storage, the ``/auth/me`` + ``/api/me`` profile endpoints, and logout.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime

from auth_deps import (
    authorize_worker_or_member,
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
from oauth import FRONTEND_URL, GITHUB_CLIENT_ID, create_session, make_authorize_url
from sqlalchemy import delete, select
from sqlmodel import SQLModel

router = APIRouter()


class CodeExchangeRequest(SQLModel):
    code: str
    state: str


class ClaudeCredentialsRequest(SQLModel):
    guild_id: str
    credentials_blob: str  # base64-encoded tar.gz of ~/.claude/


@router.get("/auth/github/login")
async def github_login():
    """Start the GitHub OAuth flow. Returns the authorization URL."""
    if not GITHUB_CLIENT_ID:
        raise HTTPException(
            status_code=500, detail="GitHub OAuth not configured (missing GITHUB_CLIENT_ID)"
        )
    return {"url": make_authorize_url()}


@router.post("/auth/github/exchange")
async def github_exchange(body: CodeExchangeRequest):
    """Exchange a GitHub OAuth code+state for a login session. Called by the frontend."""
    return await create_session(body.code, body.state)


@router.get("/auth/github/callback")
async def github_callback(code: str = Query(...), state: str = Query(...)):
    """Legacy backend OAuth callback — redirects to the frontend with session params in the query string."""
    payload = await create_session(code, state)
    qs = urllib.parse.urlencode(payload)
    return RedirectResponse(url=f"{FRONTEND_URL}/?{qs}")


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
        result = await db.execute(select(Guild.github_user_id).where(Guild.id == guild_id))
        github_user_id_val = result.scalar_one_or_none()
        if not github_user_id_val:
            raise HTTPException(status_code=404, detail="No GitHub account linked to this guild")
        result = await db.execute(
            select(GithubToken.access_token, GithubToken.github_username).where(
                GithubToken.github_user_id == github_user_id_val
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
        result = await db.execute(
            select(ClaudeCredentials).where(ClaudeCredentials.guild_id == guild_id)
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
        result = await db.execute(
            select(ClaudeCredentials).where(ClaudeCredentials.guild_id == data.guild_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.credentials_blob = data.credentials_blob
            row.updated_at = now
        else:
            db.add(
                ClaudeCredentials(
                    guild_id=data.guild_id,
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
    """Return the current user's profile + their guild memberships.

    Includes legacy guilds owned via ``guilds.github_user_id`` even when no
    ``guild_members`` row exists, so the UI never sees a logged-in owner with
    zero guilds just because their backfill row is missing.
    """
    db = await get_db()
    try:
        u_res = await db.execute(select(User).where(User.id == github_user_id))
        user = u_res.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        explicit = await db.execute(
            select(GuildMember.guild_id, GuildMember.role, Guild.name)
            .join(Guild, Guild.id == GuildMember.guild_id)
            .where(GuildMember.user_id == github_user_id)
        )
        memberships = {
            row.guild_id: {
                "guild_id": row.guild_id,
                "guild_name": row.name,
                "role": row.role,
            }
            for row in explicit.fetchall()
        }
        legacy = await db.execute(
            select(Guild.id, Guild.name).where(Guild.github_user_id == github_user_id)
        )
        for row in legacy.fetchall():
            memberships.setdefault(
                row.id,
                {"guild_id": row.id, "guild_name": row.name, "role": "owner"},
            )
        return {
            "user": {
                "id": user.id,
                "github_id": user.github_id,
                "github_login": user.github_login,
                "email": user.email,
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
            },
            "memberships": list(memberships.values()),
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
