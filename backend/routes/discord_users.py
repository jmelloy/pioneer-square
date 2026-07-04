"""CRUD for the ``discord_users`` GitHub-login -> Discord-user-ID mapping.

Populated manually by any authenticated user (there is no automated
discovery of GitHub <-> Discord identity). ``discord_notifier.mention_or_login``
reads this table to turn a GitHub login into a real ``<@id>`` mention,
falling back to a plain ``@login`` string when no row exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

from auth_deps import require_user
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import DiscordUser
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


class DiscordUserUpsert(BaseModel):
    discord_user_id: str = Field(min_length=1, max_length=64)


def _serialize(row: DiscordUser) -> dict:
    return {
        "github_login": row.github_login,
        "discord_user_id": row.discord_user_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/api/discord-users")
async def list_discord_users(
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """List every GitHub login -> Discord user ID mapping."""
    result = await db.exec(select(DiscordUser).order_by(col(DiscordUser.github_login)))
    return [_serialize(row) for row in result.all()]


@router.get("/api/discord-users/{github_login}")
async def get_discord_user(
    github_login: str,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Return the Discord mapping for a single GitHub login, or 404."""
    result = await db.exec(
        select(DiscordUser).where(col(DiscordUser.github_login) == github_login.lower())
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="No Discord mapping for this GitHub login")
    return _serialize(row)


@router.put("/api/discord-users/{github_login}")
async def upsert_discord_user(
    github_login: str,
    data: DiscordUserUpsert,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Create or update the Discord mapping for a GitHub login."""
    login = github_login.lower()
    now = datetime.now(UTC)
    stmt = (
        pg_insert(DiscordUser)
        .values(
            github_login=login,
            discord_user_id=data.discord_user_id,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["github_login"],
            set_={"discord_user_id": data.discord_user_id, "updated_at": now},
        )
    )
    await db.exec(stmt)
    await db.commit()
    result = await db.exec(select(DiscordUser).where(col(DiscordUser.github_login) == login))
    return _serialize(result.one())


@router.delete("/api/discord-users/{github_login}")
async def delete_discord_user(
    github_login: str,
    github_user_id: str = Depends(require_user),
    db: AsyncSession = Depends(get_db_dep),
):
    """Remove the Discord mapping for a GitHub login."""
    login = github_login.lower()
    await db.exec(delete(DiscordUser).where(col(DiscordUser.github_login) == login))
    await db.commit()
    return {"status": "removed", "github_login": login}
