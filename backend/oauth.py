"""GitHub OAuth helpers — code-exchange + user-info fetch + session creation.

The HTTP-level routes that call into here live in ``routes/auth.py``; this
module owns the side-effects (DB writes for github_tokens, users, and
user_sessions) plus the in-memory CSRF state set used to validate callbacks.

Foreman GitHub *tools* (commenting on PRs, etc.) live in ``foreman/tools.py``
and are unrelated to this OAuth flow.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from database import get_db
from fastapi import HTTPException
from models import GithubToken, User, UserSession
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

# ---------------------------------------------------------------------------
# Config (read at import time from environment)
# ---------------------------------------------------------------------------

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_URI = os.environ.get("GITHUB_REDIRECT_URI", "http://localhost:5173/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# Short-lived state tokens for OAuth CSRF protection. In-memory only; will be
# lost on backend restart. See CODE_REVIEW.md H2 for the planned DB persistence.
oauth_states: set[str] = set()


# ---------------------------------------------------------------------------
# GitHub HTTP calls (sync, run via asyncio.to_thread by callers)
# ---------------------------------------------------------------------------


def _gh_exchange_code(code: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_REDIRECT_URI,
        }
    ).encode()
    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _gh_get_user(token: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


def make_authorize_url() -> str:
    """Return the GitHub authorize URL with a fresh state token registered."""
    state = secrets.token_urlsafe(16)
    oauth_states.add(state)
    params = urllib.parse.urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": GITHUB_REDIRECT_URI,
            "scope": "repo read:org project",
            "state": state,
        }
    )
    return f"https://github.com/login/oauth/authorize?{params}"


async def create_session(code: str, state: str) -> dict:
    """Exchange an OAuth code+state for a login session.

    Returns the session payload dict the frontend uses to populate
    localStorage.  Persists/refreshes the ``github_tokens``, ``users``, and
    ``user_sessions`` rows.
    """
    if state not in oauth_states:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    oauth_states.discard(state)

    try:
        token_data = await asyncio.to_thread(_gh_exchange_code, code)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {exc}")

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=502, detail=f"No access_token in GitHub response: {token_data}"
        )

    try:
        user_data = await asyncio.to_thread(_gh_get_user, access_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub user fetch failed: {exc}")

    github_user_id = str(user_data["id"])
    github_username = user_data.get("login", "")
    display_name = user_data.get("name") or ""
    avatar_url = user_data.get("avatar_url") or ""
    email = user_data.get("email") or None
    login_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC).isoformat()

    db = await get_db()
    try:
        stmt = sqlite_insert(GithubToken).values(
            github_user_id=github_user_id,
            github_username=github_username,
            access_token=access_token,
            token_type=token_data.get("token_type", "bearer"),
            scope=token_data.get("scope", ""),
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["github_user_id"],
            set_={
                "github_username": stmt.excluded.github_username,
                "access_token": stmt.excluded.access_token,
                "token_type": stmt.excluded.token_type,
                "scope": stmt.excluded.scope,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await db.execute(stmt)

        user_stmt = sqlite_insert(User).values(
            id=github_user_id,
            github_id=github_user_id,
            github_login=github_username,
            email=email,
            display_name=display_name or None,
            avatar_url=avatar_url or None,
            created_at=now,
            updated_at=now,
        )
        user_stmt = user_stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "github_login": user_stmt.excluded.github_login,
                "email": user_stmt.excluded.email,
                "display_name": user_stmt.excluded.display_name,
                "avatar_url": user_stmt.excluded.avatar_url,
                "updated_at": user_stmt.excluded.updated_at,
            },
        )
        await db.execute(user_stmt)
        db.add(UserSession(token=login_token, github_user_id=github_user_id, created_at=now))
        await db.commit()
    finally:
        await db.close()

    return {
        "login_token": login_token,
        "gh_token": access_token,
        "gh_user_id": github_user_id,
        "gh_login": github_username,
        "gh_name": user_data.get("name") or "",
        "gh_avatar": user_data.get("avatar_url") or "",
    }
