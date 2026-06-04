"""Push notification token registration.

The iOS app (see ``ios/PioneerSquare/PushManager.swift``) receives an APNs
device token after the user grants notification permission and POSTs it
here. The token is keyed to the authenticated GitHub user so server-side
code can fan a notification out to every device that user has signed in
on. Re-registering the same token bumps ``last_seen_at`` so we can prune
stale rows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from auth_deps import require_user
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import PushToken
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


_SUPPORTED_PLATFORMS = {"ios"}


class RegisterRequest(BaseModel):
    token: str = Field(min_length=1, max_length=400)
    platform: str = "ios"


@router.post("/api/push/register")
async def register_push_token(
    body: RegisterRequest,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
) -> dict[str, str]:
    """Register or refresh a device token for the authenticated user.

    Returns ``{"status": "ok"}`` on success. Re-registering the same token
    is idempotent and bumps ``last_seen_at``; the row may also move between
    users if a device is signed into a new account.
    """
    if body.platform not in _SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platform: {body.platform!r}",
        )

    now = datetime.now(UTC)
    stmt = (
        pg_insert(PushToken)
        .values(
            token=body.token,
            user_id=github_user_id,
            platform=body.platform,
            created_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=["token"],
            set_={
                "user_id": github_user_id,
                "platform": body.platform,
                "last_seen_at": now,
            },
        )
    )
    await db.exec(stmt)
    await db.commit()
    return {"status": "ok"}


class UnregisterRequest(BaseModel):
    token: str = Field(min_length=1, max_length=400)


@router.delete("/api/push/register")
async def unregister_push_token(
    body: UnregisterRequest,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
) -> dict[str, str]:
    """Delete the caller's row for ``token``.

    Only deletes when the token is owned by the authenticated user, so one
    user can't silence another's device by guessing its token.
    """
    await db.exec(
        delete(PushToken).where(
            PushToken.token == body.token,
            PushToken.user_id == github_user_id,
        )
    )
    await db.commit()
    return {"status": "ok"}


@router.get("/api/push/tokens")
async def list_my_tokens(
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
) -> dict[str, list[dict]]:
    """List the caller's registered devices.

    Useful for a future "logged-in devices" settings screen and for
    debugging — exposes only the caller's own rows.
    """
    result = await db.exec(
        select(
            PushToken.token,
            PushToken.platform,
            PushToken.created_at,
            PushToken.last_seen_at,
        )
        .where(PushToken.user_id == github_user_id)
        .order_by(PushToken.last_seen_at.desc())
    )
    rows = result.all()
    return {
        "tokens": [
            {
                # Don't echo the full token back — only the suffix is enough
                # for the user to recognise which device this is.
                "token_suffix": row.token[-8:],
                "platform": row.platform,
                "created_at": row.created_at.isoformat(),
                "last_seen_at": row.last_seen_at.isoformat(),
            }
            for row in rows
        ]
    }
