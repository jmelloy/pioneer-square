"""Authentication and identity routes.

Covers GitHub OAuth (login/exchange/callback/token), Claude credential
storage, the ``/auth/me`` + ``/api/me`` profile endpoints, and logout.
"""

from __future__ import annotations

import os
import secrets
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
from sqlalchemy import delete, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from utils import generate_guild_id

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
        owner_res = await db.exec(
            select(col(GuildMember.user_id))
            .where(col(GuildMember.guild_id) == guild_pk, col(GuildMember.role) == "owner")
            .limit(1)
        )
        owner_user_id = owner_res.one_or_none()
        if not owner_user_id:
            raise HTTPException(status_code=404, detail="No GitHub account linked to this guild")
        result = await db.exec(
            select(col(GithubToken.access_token), col(GithubToken.github_username)).where(
                col(GithubToken.github_user_id) == owner_user_id
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
        result = await db.exec(
            select(ClaudeCredentials).where(col(ClaudeCredentials.guild_id) == guild_pk)
        )
        row = result.one_or_none()
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
    now = datetime.now(UTC)
    db = await get_db()
    try:
        guild_pk = await get_guild_pk(db, data.guild_id)
        if guild_pk is None:
            raise HTTPException(status_code=404, detail="Guild not found")
        result = await db.exec(
            select(ClaudeCredentials).where(col(ClaudeCredentials.guild_id) == guild_pk)
        )
        row = result.one_or_none()
        if row:
            row.credentials_blob = data.credentials_blob
            row.updated_at = now
        else:
            db.add(
                ClaudeCredentials(
                    guild_id=guild_pk,
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
        result = await db.exec(
            select(
                col(GithubToken.github_user_id),
                col(GithubToken.github_username),
                col(GithubToken.scope),
            ).where(col(GithubToken.github_user_id) == github_user_id)
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
        u_res = await db.exec(select(User).where(col(User.id) == github_user_id))
        user = u_res.one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        members_res = await db.exec(
            select(col(Guild.guild_id), col(GuildMember.role), col(Guild.name))
            .join(Guild, col(Guild.id) == col(GuildMember.guild_id))
            .where(col(GuildMember.user_id) == github_user_id)
        )
        memberships = [
            {"guild_id": row.guild_id, "guild_name": row.name, "role": row.role}
            for row in members_res.all()
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


@router.post("/auth/guest")
async def guest_login():
    """Create a dev session without GitHub OAuth.

    Only available when TEST_MODE=1 is set. Idempotent: always creates a new
    session token but reuses the same dev-guest user and guild across calls.
    """
    if not os.environ.get("TEST_MODE"):
        raise HTTPException(status_code=403, detail="Test mode not enabled (set TEST_MODE=1)")

    now = datetime.now(UTC)
    guest_user_id = "dev-guest"
    login_token = secrets.token_urlsafe(32)

    db = await get_db()
    try:
        # Upsert github_tokens (required FK target for user_sessions)
        gh_stmt = pg_insert(GithubToken).values(
            github_user_id=guest_user_id,
            github_username="dev-guest",
            access_token="dev-no-token",
            token_type="bearer",
            scope="",
            created_at=now,
            updated_at=now,
        )
        gh_stmt = gh_stmt.on_conflict_do_update(
            index_elements=["github_user_id"],
            set_={"updated_at": gh_stmt.excluded.updated_at},
        )
        await db.exec(gh_stmt)

        # Upsert user
        user_stmt = pg_insert(User).values(
            id=guest_user_id,
            github_id=guest_user_id,
            github_login="dev-guest",
            email=None,
            display_name="Dev Guest",
            avatar_url=None,
            created_at=now,
            updated_at=now,
        )
        user_stmt = user_stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"updated_at": user_stmt.excluded.updated_at},
        )
        await db.exec(user_stmt)

        # Fresh session each call (so re-login after logout works)
        db.add(UserSession(token=login_token, github_user_id=guest_user_id, created_at=now))

        # Find or create the persistent dev guild
        _not_deleted = or_(col(Guild.deleted_at).is_(None), col(Guild.deleted_at) == "")
        guild_res = await db.exec(
            select(col(Guild.guild_id))
            .where(col(Guild.github_user_id) == guest_user_id, _not_deleted)
            .limit(1)
        )
        guild_id = guild_res.one_or_none()

        if not guild_id:
            existing_res = await db.exec(select(col(Guild.guild_id)).where(_not_deleted))
            existing_ids = set(existing_res.all())
            guild_id = generate_guild_id(name="dev", existing_ids=existing_ids)
            new_guild = Guild(
                guild_id=guild_id,
                created_at=now,
                name="Dev Workshop",
                github_user_id=guest_user_id,
            )
            db.add(new_guild)
            await db.flush()
            db.add(
                GuildMember(
                    guild_id=new_guild.id or 0,
                    user_id=guest_user_id,
                    role="owner",
                    created_at=now,
                )
            )

        await db.commit()
    finally:
        await db.close()

    return {
        "login_token": login_token,
        "guild_id": guild_id,
        "gh_user_id": guest_user_id,
        "gh_login": "dev-guest",
        "gh_name": "Dev Guest",
        "gh_avatar": "",
    }


@router.delete("/auth/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(http_bearer)):
    """Invalidate the current login_token."""
    if credentials:
        db = await get_db()
        try:
            await db.exec(
                delete(UserSession).where(col(UserSession.token) == credentials.credentials)
            )
            await db.commit()
        finally:
            await db.close()
    return {"status": "logged_out"}
