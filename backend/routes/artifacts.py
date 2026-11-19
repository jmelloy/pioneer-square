"""Artifact (image/HTML/text/PDF) uploads: ``POST /guilds/{guild_id}/artifacts``.

Workers and guild members upload via multipart; the backend validates and stores the
bytes in S3 (see ``artifact_service``) and records an ``artifacts`` row.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import artifact_service
from auth_deps import get_guild_pk, require_worker_or_member_path
from database import get_db_dep
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from models import Artifact, Task, Worker
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


def _link_text(name: str | None) -> str:
    """Filename safe for use as markdown link text."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch not in "[]()\n\r").strip()
    return base or "artifact"


def artifact_uri(a: Artifact) -> str:
    # Non-image types carry ``?type=`` so renderers know how to display without a lookup.
    return f"artifact://{a.id}" + ("" if a.type == "image" else f"?type={a.type}")


def _payload(a: Artifact, name: str | None = None) -> dict:
    uri = artifact_uri(a)
    return {
        "id": a.id,
        "key": a.key,
        # Stored url may be a legacy presigned link; always hand out the permanent one.
        "url": artifact_service.public_url(a.id),
        "contentType": a.content_type,
        "type": a.type,
        "sizeBytes": a.size_bytes,
        "taskId": a.task_id,
        "uploadedByWorkerId": a.uploaded_by_worker_id,
        "createdAt": a.created_at.isoformat(),
        "artifactUri": uri,
        # Reference in message content; the frontend resolves artifact:// links.
        "markdown": f"[{_link_text(name)}]({uri})",
    }


@router.post("/guilds/{guild_id}/artifacts")
async def upload_artifact(
    guild_id: str,
    file: UploadFile = File(...),
    task_id: str | None = Form(default=None),
    type: str | None = Form(default=None),  # optional; must match the file's content type
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
        if type:
            actual = artifact_service.artifact_type(
                artifact_service.validate(data, file.content_type, limit)
            )
            if type != actual:
                raise artifact_service.ArtifactError(
                    f"File is a {actual} artifact, not {type}", 415
                )
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
        type=obj.type,
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
    return _payload(row)


@router.get("/api/artifacts/{artifact_id}/public")
async def public_artifact(artifact_id: str, db: AsyncSession = Depends(get_db_dep)):
    """Permanent public link. Unauthenticated: the random UUID is the capability, like the
    S3 presigned URL it replaces. Streams from S3 so bucket details stay private."""
    row = (await db.exec(select(Artifact).where(col(Artifact.id) == artifact_id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        body = await asyncio.to_thread(artifact_service.open_object, row.key)
    except artifact_service.ArtifactError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return StreamingResponse(
        body.iter_chunks(64 * 1024),  # sync iterator; Starlette runs it in a threadpool
        media_type=row.content_type,  # validated type from upload
        headers={
            # Opaque origin: uploaded HTML may run scripts but can't touch app cookies/storage.
            **({"Content-Security-Policy": "sandbox allow-scripts"} if row.type == "html" else {}),
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            **({"Content-Length": str(row.size_bytes)} if row.size_bytes else {}),
        },
    )
