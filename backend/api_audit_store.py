"""Private-S3 archive for Foreman API audit payloads.

``api_request_log`` keeps queryable metadata (attribution, token usage, timestamps) in
Postgres. The bulky request (system + messages + extra) and response bodies are gzip-compressed
JSON objects in a private S3 bucket; rows hold only object references.

Configuration (credentials from the standard boto3 chain / ECS task role):

- ``PIONEER_AUDIT_BUCKET``   bucket name. Unset => archival disabled and payloads are stored
  inline in the legacy JSON columns (local dev / rollout fallback).
- ``PIONEER_AUDIT_PREFIX``   key prefix (default ``foreman-api-audit``)
- ``PIONEER_AUDIT_REGION``   optional region override
- ``PIONEER_AUDIT_UPLOAD_ATTEMPTS``  upload attempts before falling back inline (default 3)

Reads are lazy and explicit (``read_payloads``); metadata queries never touch S3.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "foreman-api-audit"
DEFAULT_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.2  # seconds; doubles per attempt


class AuditStoreError(Exception):
    """Base error for audit archive failures."""


class AuditUploadError(AuditStoreError):
    """Upload failed after all retries."""


class AuditObjectExpired(AuditStoreError):
    """The referenced object no longer exists (lifecycle expiry or manual deletion)."""


class AuditAccessDenied(AuditStoreError):
    """Caller is not authorized to read this row's payload."""


@dataclass(frozen=True)
class AuditConfig:
    bucket: str
    prefix: str
    region: str | None
    attempts: int


@dataclass(frozen=True)
class ObjectRef:
    key: str
    size_bytes: int
    sha256: str


def load_config() -> AuditConfig | None:
    bucket = os.environ.get("PIONEER_AUDIT_BUCKET")
    if not bucket:
        return None
    return AuditConfig(
        bucket=bucket,
        prefix=os.environ.get("PIONEER_AUDIT_PREFIX", DEFAULT_PREFIX).strip("/"),
        region=os.environ.get("PIONEER_AUDIT_REGION") or None,
        attempts=max(1, int(os.environ.get("PIONEER_AUDIT_UPLOAD_ATTEMPTS", DEFAULT_ATTEMPTS))),
    )


_client: Any = None


def _s3(cfg: AuditConfig) -> Any:
    global _client
    if _client is None:
        import boto3  # noqa: PLC0415

        _client = boto3.client("s3", region_name=cfg.region)
    return _client


def encode_payload(payload: Any) -> bytes:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
    return gzip.compress(raw, mtime=0)


def decode_payload(data: bytes) -> Any:
    return json.loads(gzip.decompress(data))


def build_key(
    cfg: AuditConfig, guild_pk: int | None, kind: str, when: datetime | None = None
) -> str:
    when = when or datetime.now(UTC)
    guild = str(guild_pk) if guild_pk is not None else "none"
    return f"{cfg.prefix}/{guild}/{when:%Y/%m/%d}/{uuid.uuid4().hex}/{kind}.json.gz"


async def put_payload(
    cfg: AuditConfig, key: str, payload: Any, *, sleep=asyncio.sleep
) -> ObjectRef:
    """Compress and upload ``payload`` to ``key`` with exponential-backoff retries.

    Puts are idempotent for a fixed key, so retrying is safe. Raises ``AuditUploadError``.
    """
    body = encode_payload(payload)
    ref = ObjectRef(key=key, size_bytes=len(body), sha256=hashlib.sha256(body).hexdigest())

    def _put() -> None:
        _s3(cfg).put_object(
            Bucket=cfg.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ContentEncoding="gzip",
            ServerSideEncryption="AES256",
        )

    last: Exception | None = None
    for attempt in range(cfg.attempts):
        try:
            await asyncio.to_thread(_put)
            return ref
        except Exception as exc:  # botocore errors
            last = exc
            logger.warning(
                "audit upload failed key=%s attempt=%d/%d: %s", key, attempt + 1, cfg.attempts, exc
            )
            if attempt + 1 < cfg.attempts:
                await sleep(RETRY_BASE_DELAY * (2**attempt))
    raise AuditUploadError(f"upload of {key} failed after {cfg.attempts} attempts") from last


async def get_payload(cfg: AuditConfig, key: str) -> Any:
    def _get() -> bytes:
        return _s3(cfg).get_object(Bucket=cfg.bucket, Key=key)["Body"].read()

    try:
        data = await asyncio.to_thread(_get)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            raise AuditObjectExpired(key) from exc
        raise AuditStoreError(f"read of {key} failed: {exc}") from exc
    return decode_payload(data)


async def archive_request(
    *, guild_pk: int | None, system: list, messages: list, extra: dict | None
) -> dict[str, Any]:
    """Return ``ApiRequestLog`` column values for a request payload.

    Archived to S3 when configured; on disabled archival or upload failure the payload is kept
    inline in the legacy columns so the audit record is never lost and the LLM call never fails.
    """
    cfg = load_config()
    if cfg is not None:
        try:
            ref = await put_payload(
                cfg,
                build_key(cfg, guild_pk, "request"),
                {"system": system, "messages": messages, "extra": extra or None},
            )
            return {
                "request_object_key": ref.key,
                "request_object_bytes": ref.size_bytes,
                "request_sha256": ref.sha256,
            }
        except AuditUploadError:
            logger.error("audit request archival failed; storing inline", exc_info=True)
    return {"system": system, "messages": messages, "extra": extra or None}


async def archive_response(*, guild_pk: int | None, response: dict) -> dict[str, Any]:
    """Return ``ApiRequestLog`` column values for a response payload (see ``archive_request``)."""
    cfg = load_config()
    if cfg is not None:
        try:
            ref = await put_payload(cfg, build_key(cfg, guild_pk, "response"), response)
            return {
                "response_object_key": ref.key,
                "response_object_bytes": ref.size_bytes,
                "response_sha256": ref.sha256,
            }
        except AuditUploadError:
            logger.error("audit response archival failed; storing inline", exc_info=True)
    return {"response": response}


async def read_payloads(row: Any, *, guild_pk: int | None, include_response: bool = True) -> dict:
    """Lazily load a row's payloads (S3 first, legacy inline columns as fallback).

    ``guild_pk`` is the caller's guild; rows belonging to another guild (or with no guild) are
    refused. Returns ``{"system", "messages", "extra", "response"}``; a payload whose object has
    expired yields ``None`` plus an entry in ``"expired"``.
    """
    if row.guild_id is None or row.guild_id != guild_pk:
        raise AuditAccessDenied("api_request_log row belongs to a different guild")
    out: dict[str, Any] = {
        "system": row.system,
        "messages": row.messages,
        "extra": row.extra,
        "response": row.response,
        "expired": [],
    }
    cfg = load_config()
    wanted = [("request", row.request_object_key)]
    if include_response:
        wanted.append(("response", row.response_object_key))
    for kind, key in wanted:
        if not key:
            continue  # legacy row: inline columns already populated
        if cfg is None:
            raise AuditStoreError("audit bucket is not configured")
        try:
            data = await get_payload(cfg, key)
        except AuditObjectExpired:
            out["expired"].append(kind)
            continue
        if kind == "request":
            out["system"] = data.get("system")
            out["messages"] = data.get("messages")
            out["extra"] = data.get("extra")
        else:
            out["response"] = data
    return out


# --- Metadata queries ---------------------------------------------------------------------

_METADATA_FIELDS = (
    "id",
    "request_id",
    "created_at",
    "completed_at",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "stop_reason",
    "task_id",
    "guild_id",
    "user_id",
    "trigger",
    "request_object_bytes",
    "response_object_bytes",
)


async def list_metadata(db: Any, *, guild_pk: int, limit: int = 100) -> list[dict[str, Any]]:
    """Recent rows for a guild, metadata only: payload columns and S3 are never touched."""
    from models import ApiRequestLog
    from sqlmodel import col, select

    cols = [col(getattr(ApiRequestLog, f)) for f in _METADATA_FIELDS]
    rows = await db.exec(
        select(*cols)
        .where(col(ApiRequestLog.guild_id) == guild_pk)
        .order_by(col(ApiRequestLog.id).desc())
        .limit(limit)
    )
    return [dict(zip(_METADATA_FIELDS, r, strict=True)) for r in rows.all()]


# --- Backfill ---------------------------------------------------------------------------


async def backfill_legacy_rows(db: Any, *, batch_size: int = 100) -> int:
    """Archive rows that still hold inline payloads. Idempotent and resumable.

    Only rows with no object reference and some inline payload are selected; each row's update
    is guarded by ``request_object_key IS NULL AND response_object_key IS NULL`` so concurrent
    or repeated runs cannot double-process. Inline columns are cleared only after a successful
    upload. Returns the number of rows archived in this call (0 when nothing is left).
    """
    from models import ApiRequestLog
    from sqlalchemy import update as sa_update
    from sqlmodel import col, or_, select

    cfg = load_config()
    if cfg is None:
        raise AuditStoreError("PIONEER_AUDIT_BUCKET must be set to backfill")
    rows = (
        await db.exec(
            select(ApiRequestLog)
            .where(
                col(ApiRequestLog.request_object_key).is_(None),
                col(ApiRequestLog.response_object_key).is_(None),
                or_(
                    col(ApiRequestLog.system).is_not(None),
                    col(ApiRequestLog.messages).is_not(None),
                    col(ApiRequestLog.response).is_not(None),
                ),
            )
            .order_by(col(ApiRequestLog.id))
            .limit(batch_size)
        )
    ).all()
    done = 0
    for row in rows:
        values: dict[str, Any] = {}
        try:
            if row.system is not None or row.messages is not None:
                ref = await put_payload(
                    cfg,
                    build_key(cfg, row.guild_id, "request", row.created_at),
                    {"system": row.system, "messages": row.messages, "extra": row.extra},
                )
                values |= {
                    "request_object_key": ref.key,
                    "request_object_bytes": ref.size_bytes,
                    "request_sha256": ref.sha256,
                    "system": None,
                    "messages": None,
                    "extra": None,
                }
            if row.response is not None:
                ref = await put_payload(
                    cfg, build_key(cfg, row.guild_id, "response", row.created_at), row.response
                )
                values |= {
                    "response_object_key": ref.key,
                    "response_object_bytes": ref.size_bytes,
                    "response_sha256": ref.sha256,
                    "response": None,
                }
        except AuditUploadError:
            logger.error("backfill skipped row id=%s", row.id)
            continue
        res = await db.exec(
            sa_update(ApiRequestLog)
            .where(
                col(ApiRequestLog.id) == row.id,
                col(ApiRequestLog.request_object_key).is_(None),
                col(ApiRequestLog.response_object_key).is_(None),
            )
            .values(**values)
        )
        done += res.rowcount or 0
    await db.commit()
    return done


async def _main() -> None:
    from database import AsyncSessionLocal

    total = 0
    while True:
        async with AsyncSessionLocal() as db:
            n = await backfill_legacy_rows(db)
        total += n
        if n == 0:
            break
    print(f"archived {total} api_request_log rows")


if __name__ == "__main__":
    asyncio.run(_main())
