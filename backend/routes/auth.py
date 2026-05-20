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
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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


@router.post("/auth/guest")
async def guest_login():
    """Create a dev session without GitHub OAuth.

    Only available when TEST_MODE=1 is set. Idempotent: always creates a new
    session token but reuses the same dev-guest user and guild across calls.
    """
    if not os.environ.get("TEST_MODE"):
        raise HTTPException(status_code=403, detail="Test mode not enabled (set TEST_MODE=1)")

    now = datetime.now(UTC).isoformat()
    guest_user_id = "dev-guest"
    login_token = secrets.token_urlsafe(32)

    db = await get_db()
    try:
        # Upsert github_tokens (required FK target for user_sessions)
        gh_stmt = sqlite_insert(GithubToken).values(
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
        await db.execute(gh_stmt)

        # Upsert user
        user_stmt = sqlite_insert(User).values(
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
        await db.execute(user_stmt)

        # Fresh session each call (so re-login after logout works)
        db.add(UserSession(token=login_token, github_user_id=guest_user_id, created_at=now))

        # Find or create the persistent dev guild
        guild_res = await db.execute(
            text(
                "SELECT guild_id FROM guilds"
                " WHERE github_user_id = :uid AND (deleted_at IS NULL OR deleted_at = '')"
                " LIMIT 1"
            ),
            {"uid": guest_user_id},
        )
        guild_id = guild_res.scalar_one_or_none()

        if not guild_id:
            existing_res = await db.execute(
                text("SELECT guild_id FROM guilds WHERE deleted_at IS NULL OR deleted_at = ''")
            )
            existing_ids = {row[0] for row in existing_res.fetchall()}
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
                    guild_pk=new_guild.id,
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


@router.get("/api/user/logins")
async def list_logins(github_user_id: str = Depends(require_user)):
    """Return the list of connected OAuth providers for the current user.

    Currently GitHub is the only supported provider; the response is structured
    as a provider list so additional providers can be added without breaking the
    API shape.
    """
    db = await get_db()
    try:
        result = await db.execute(
            select(
                GithubToken.github_username,
                GithubToken.scope,
                GithubToken.created_at,
                GithubToken.updated_at,
            ).where(GithubToken.github_user_id == github_user_id)
        )
        row = result.first()
        logins = []
        if row:
            logins.append(
                {
                    "provider": "github",
                    "username": row.github_username,
                    "scope": row.scope,
                    "connected_at": row.created_at,
                    "updated_at": row.updated_at,
                }
            )
        return {"logins": logins}
    finally:
        await db.close()


@router.delete("/api/user/logins/{provider}")
async def delete_login(provider: str, github_user_id: str = Depends(require_user)):
    """Disconnect an OAuth provider from the current user's account.

    Returns HTTP 404 if the user has no login for the requested provider.
    Guards against removing the last login — if this would leave the user with
    zero logins they would be permanently locked out, so we return HTTP 400.
    Currently only ``github`` is supported; passing any other provider returns
    HTTP 404.
    """
    if provider != "github":
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider!r}")

    db = await get_db()
    try:
        # Verify the requested provider login actually exists for this user.
        if provider == "github":
            existing_res = await db.execute(
                select(GithubToken).where(GithubToken.github_user_id == github_user_id)
            )
            if existing_res.scalar_one_or_none() is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No {provider!r} login found for this user.",
                )

        # Count logins per provider so we can compute how many would remain after
        # this deletion.  Add other provider table counts to total_logins when
        # they land.
        github_count_res = await db.execute(
            select(func.count())
            .select_from(GithubToken)
            .where(GithubToken.github_user_id == github_user_id)
        )
        github_count = github_count_res.scalar_one() or 0

        # total_logins = sum across all providers (only github today).
        total_logins = github_count
        provider_count = github_count if provider == "github" else 0

        remaining = total_logins - provider_count
        if remaining == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot disconnect your only login method — you would be locked out.",
            )

        # Delete only the token(s) for the requested provider.
        if provider == "github":
            await db.execute(
                delete(GithubToken).where(GithubToken.github_user_id == github_user_id)
            )
        await db.execute(delete(UserSession).where(UserSession.github_user_id == github_user_id))
        await db.commit()
    finally:
        await db.close()
    return {"ok": True}


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
