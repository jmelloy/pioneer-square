"""POST /api/discord/connect — redeem a one-time token from /connect-account.

Links the authenticated Pioneer Square user to the Discord account that
generated the token via the ``/connect-account`` slash command (see
``routes/discord.py``). Tokens are single-use and expire 15 minutes after
creation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from auth_deps import require_user
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import DiscordAccountLink, DiscordConnectToken
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


class DiscordConnectRequest(BaseModel):
    token: str = Field(min_length=1)


@router.post("/api/discord/connect")
async def connect_discord_account(
    data: DiscordConnectRequest,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Redeem a one-time /connect-account token and link the Discord account
    to the currently authenticated Pioneer Square user."""
    result = await db.exec(
        select(DiscordConnectToken).where(col(DiscordConnectToken.token) == data.token)
    )
    token_row = result.one_or_none()
    if not token_row:
        raise HTTPException(status_code=404, detail="Invalid or unknown connect token")
    if token_row.used_at is not None:
        raise HTTPException(status_code=400, detail="This connect link has already been used")
    if token_row.expires_at < datetime.now(UTC):
        raise HTTPException(status_code=400, detail="This connect link has expired")

    now = datetime.now(UTC)
    token_row.used_at = now
    db.add(token_row)

    stmt = pg_insert(DiscordAccountLink).values(
        ps_user_id=github_user_id,
        discord_user_id=token_row.discord_user_id,
        discord_username=token_row.discord_username,
        created_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["discord_user_id"],
        set_={
            "ps_user_id": stmt.excluded.ps_user_id,
            "discord_username": stmt.excluded.discord_username,
        },
    )
    await db.exec(stmt)
    await db.commit()

    return {
        "status": "linked",
        "discord_user_id": token_row.discord_user_id,
        "discord_username": token_row.discord_username,
    }
