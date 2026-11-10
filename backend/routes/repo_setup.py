"""Per-repository setup commands (``/guilds/{guild_id}/repos/{owner}/{repo}/setup``).

Members with the owner or member role may edit; workers and any guild member may read.
"""

from __future__ import annotations

from datetime import UTC, datetime

from auth_deps import get_guild_pk, require_member, require_worker_or_member_path
from database import get_db_dep
from fastapi import APIRouter, Depends, HTTPException
from models import RepositorySetupSettings
from pydantic import BaseModel, field_validator
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()

MAX_COMMANDS = 50
MAX_COMMAND_LEN = 2000
MIN_TIMEOUT = 1
MAX_TIMEOUT = 3600
DEFAULT_TIMEOUT = 600
_EDIT_ROLES = ("owner", "member")


class RepoSetupUpdate(BaseModel):
    setup_commands: list[str]
    timeout_seconds: int = DEFAULT_TIMEOUT

    @field_validator("setup_commands")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        cmds = [c.strip() for c in v if c and c.strip()]
        if len(cmds) > MAX_COMMANDS:
            raise ValueError(f"at most {MAX_COMMANDS} commands")
        if any(len(c) > MAX_COMMAND_LEN for c in cmds):
            raise ValueError(f"commands must be at most {MAX_COMMAND_LEN} characters")
        return cmds

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout(cls, v: int) -> int:
        if not MIN_TIMEOUT <= v <= MAX_TIMEOUT:
            raise ValueError(f"timeout_seconds must be {MIN_TIMEOUT}-{MAX_TIMEOUT}")
        return v


def _norm(repo: str) -> str:
    return repo.strip().lower()


def _payload(repo: str, row: RepositorySetupSettings | None) -> dict:
    return {
        "repo": repo,
        "setupCommands": list(row.setup_commands or []) if row else [],
        "timeoutSeconds": row.timeout_seconds if row else DEFAULT_TIMEOUT,
        "updatedAt": row.updated_at.isoformat() if row else None,
    }


async def _get_row(db: AsyncSession, guild_id: str, repo: str):
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    res = await db.exec(
        select(RepositorySetupSettings).where(
            col(RepositorySetupSettings.guild_id) == guild_pk,
            col(RepositorySetupSettings.repo) == _norm(repo),
        )
    )
    return guild_pk, res.one_or_none()


@router.get("/guilds/{guild_id}/repos/{repo:path}/setup")
async def get_repo_setup(
    guild_id: str,
    repo: str,
    _principal: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    _, row = await _get_row(db, guild_id, repo)
    return _payload(repo, row)


@router.put("/guilds/{guild_id}/repos/{repo:path}/setup")
async def put_repo_setup(
    guild_id: str,
    repo: str,
    data: RepoSetupUpdate,
    _user: str = Depends(require_member(*_EDIT_ROLES)),
    db: AsyncSession = Depends(get_db_dep),
):
    guild_pk, row = await _get_row(db, guild_id, repo)
    now = datetime.now(UTC)
    if row is None:
        row = RepositorySetupSettings(guild_id=guild_pk, repo=_norm(repo), created_at=now)
    row.setup_commands = data.setup_commands
    row.timeout_seconds = data.timeout_seconds
    row.updated_at = now
    db.add(row)
    await db.commit()
    return _payload(repo, row)


@router.delete("/guilds/{guild_id}/repos/{repo:path}/setup")
async def delete_repo_setup(
    guild_id: str,
    repo: str,
    _user: str = Depends(require_member(*_EDIT_ROLES)),
    db: AsyncSession = Depends(get_db_dep),
):
    _, row = await _get_row(db, guild_id, repo)
    if row is not None:
        await db.delete(row)
        await db.commit()
    return {"status": "deleted"}
