"""Artifact (screenshot/image) uploads: ``POST /guilds/{guild_id}/artifacts``.

Workers and guild members upload via multipart; the backend validates and stores the
bytes in S3 (see ``artifact_service``) and records an ``artifacts`` row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import artifact_service
from auth_deps import get_guild_pk, require_worker_or_member_path
from database import get_db_dep
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from models import Artifact, Task, Worker
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


def _link_text(name: str | None) -> str:
    """Filename safe for use as markdown link text."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch not in "[]()\n\r").strip()
    return base or "artifact"


def _payload(a: Artifact, name: str | None = None) -> dict:
    return {
        "id": a.id,
        "key": a.key,
        "url": a.url,
        "contentType": a.content_type,
        "sizeBytes": a.size_bytes,
        "taskId": a.task_id,
        "uploadedByWorkerId": a.uploaded_by_worker_id,
        "createdAt": a.created_at.isoformat(),
        "artifactUri": f"artifact://{a.id}",
        # Reference in message content; the frontend resolves artifact:// links.
        "markdown": f"[{_link_text(name)}](artifact://{a.id})",
    }


@router.post("/guilds/{guild_id}/artifacts")
async def upload_artifact(
    guild_id: str,
    file: UploadFile = File(...),
    task_id: str | None = Form(default=None),
    principal: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    guild_pk = await get_guild_pk(db, guild_id)
    if guild_pk is None:
        raise HTTPException(status_code=404, detail="Guild not found")
    if task_id:
        owner = (await db.exec(select(col(Task.guild_id)).where(col(Task.id) == task_id))).first()
        if owner != guild_pk:
            raise HTTPException(status_code=404, detail="Task not found")
    worker_id = principal.split(":", 1)[1] if principal.startswith("worker:") else None
    if (
        worker_id
        and not (await db.exec(select(col(Worker.id)).where(col(Worker.id) == worker_id))).first()
    ):
        worker_id = None

    cfg = artifact_service.load_config()
    limit = cfg.max_bytes if cfg else artifact_service.MAX_ARTIFACT_BYTES_DEFAULT
    data = await file.read(limit + 1)  # bounded read; validate() rejects oversize
    try:
        obj = await artifact_service.store(data, file.content_type, guild_id, task_id)
    except artifact_service.ArtifactError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    now = datetime.now(UTC)
    row = Artifact(
        id=obj.id or str(uuid.uuid4()),
        guild_id=guild_pk,
        key=obj.key,
        url=obj.url,
        content_type=obj.content_type,
        size_bytes=obj.size_bytes,
        uploaded_by_worker_id=worker_id,
        task_id=task_id,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.commit()
    return _payload(row, file.filename)


@router.get("/guilds/{guild_id}/artifacts/{artifact_id}")
async def get_artifact(
    guild_id: str,
    artifact_id: str,
    _principal: str = Depends(require_worker_or_member_path),
    db: AsyncSession = Depends(get_db_dep),
):
    guild_pk = await get_guild_pk(db, guild_id)
    row = (
        await db.exec(
            select(Artifact).where(
                col(Artifact.id) == artifact_id, col(Artifact.guild_id) == guild_pk
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    # Pre-signed URLs expire; mint a fresh one on read.
    row.url = artifact_service.refresh_url(row.key) or row.url
    return _payload(row)
