"""Private-S3 archival of ``api_request_log`` payloads (issue #1487).

The bulky ``system``/``messages``/``extra`` (request) and ``response`` (response) JSON
of each Foreman LLM call is stored as a gzip'd object in a private bucket. Postgres keeps
searchable metadata plus object references and archive state.

Lifecycle of a row's payload:

* **inline**   – no bucket configured, or not archived yet (legacy rows, failed uploads):
  the JSON columns hold the payload.
* **archived** – ``*_object_key`` set, inline columns cleared. Inline columns are cleared
  only after the upload was verified with ``head_object``.
* **expired**  – archived, ``payload_expires_at`` has passed (the S3 lifecycle rule removes
  the object); distinct from **missing** (not expired but object absent) and from
  **pending** (inline payload still awaiting a retry).

Configuration (env): ``PIONEER_API_LOG_BUCKET`` (unset = feature off, payloads stay inline),
``PIONEER_API_LOG_PREFIX`` (default ``api-request-log``), ``PIONEER_API_LOG_RETENTION_DAYS``
(default 7; the terraform lifecycle rule must expire at least this long after upload),
``PIONEER_API_LOG_REGION``.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update as sa_update
from sqlmodel import col, or_, select

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7
UPLOAD_ATTEMPTS = 3
UPLOAD_BACKOFF_SECONDS = 0.5
SWEEP_BATCH = 50

REQUEST = "request"
RESPONSE = "response"


class PayloadUnavailable(Exception):
    """Payload cannot be read; ``reason`` is ``expired`` | ``missing`` | ``error``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def bucket() -> str | None:
    return os.environ.get("PIONEER_API_LOG_BUCKET") or None


def enabled() -> bool:
    return bucket() is not None


def prefix() -> str:
    return os.environ.get("PIONEER_API_LOG_PREFIX", "api-request-log").strip("/")


def retention() -> timedelta:
    try:
        days = int(os.environ.get("PIONEER_API_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    except ValueError:
        days = DEFAULT_RETENTION_DAYS
    return timedelta(days=max(days, 1))


_client: Any = None


def _s3() -> Any:
    global _client
    if _client is None:
        import boto3  # noqa: PLC0415

        _client = boto3.client("s3", region_name=os.environ.get("PIONEER_API_LOG_REGION") or None)
    return _client


def object_key(log_id: int, created_at: datetime, kind: str) -> str:
    return f"{prefix()}/{created_at:%Y/%m/%d}/{log_id}/{kind}.json.gz"


def encode(payload: dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(payload, default=str, separators=(",", ":")).encode())


def decode(body: bytes) -> dict[str, Any]:
    return json.loads(gzip.decompress(body))


def _put_verified(key: str, body: bytes) -> None:
    client = _s3()
    client.put_object(
        Bucket=bucket(),
        Key=key,
        Body=body,
        ContentType="application/json",
        ContentEncoding="gzip",
        ServerSideEncryption="AES256",
    )
    head = client.head_object(Bucket=bucket(), Key=key)
    if head.get("ContentLength") != len(body):
        raise RuntimeError(f"verification failed for {key}: size mismatch")


async def upload(key: str, body: bytes) -> bool:
    """Upload + verify with bounded retries. Returns False (never raises) on failure."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(_put_verified, key, body)
            return True
        except Exception as exc:  # botocore/network errors
            logger.warning(
                "api_request_log upload %s failed (%d/%d): %s", key, attempt, UPLOAD_ATTEMPTS, exc
            )
            if attempt < UPLOAD_ATTEMPTS:
                await asyncio.sleep(UPLOAD_BACKOFF_SECONDS * attempt)
    return False


async def archive(db: Any, log_id: int, kind: str) -> bool:
    """Archive one direction of a row's payload. Idempotent; safe to call repeatedly.

    Returns True if the payload is (now) archived or there was nothing to archive.
    On failure the inline payload is left untouched for the sweeper to retry.
    """
    from models import ApiRequestLog  # noqa: PLC0415

    if not enabled():
        return False
    row = (
        await db.exec(select(ApiRequestLog).where(col(ApiRequestLog.id) == log_id))
    ).one_or_none()
    if row is None:
        return True
    if kind == REQUEST:
        if row.request_object_key:
            return True
        if row.system is None and row.messages is None and row.extra is None:
            return True
        payload = {"system": row.system, "messages": row.messages, "extra": row.extra}
        key_col, clear = "request_object_key", {"system": None, "messages": None, "extra": None}
    else:
        if row.response_object_key:
            return True
        if row.response is None:
            return True
        payload = {"response": row.response}
        key_col, clear = "response_object_key", {"response": None}

    key = object_key(log_id, row.created_at, kind)
    if not await upload(key, encode(payload)):
        return False
    now = datetime.now(UTC)
    await db.exec(
        sa_update(ApiRequestLog)
        .where(col(ApiRequestLog.id) == log_id)
        .values(
            **{key_col: key},
            **clear,
            payload_archived_at=now,
            payload_expires_at=now + retention(),
        )
    )
    await db.commit()
    return True


async def archive_quietly(db: Any, log_id: int, kind: str) -> None:
    """``archive`` that can never fail the caller's Foreman call."""
    try:
        await archive(db, log_id, kind)
    except Exception:
        logger.exception("api_request_log archive %s/%s failed", log_id, kind)


async def sweep_once(db: Any, after_id: int = 0, batch: int = SWEEP_BATCH) -> int:
    """Archive one batch of inline rows (backfill + failed-upload retry).

    Returns the highest id examined (0 when nothing was left), so callers can resume
    after an interruption; because archived rows no longer match, re-running is idempotent.
    Rows whose response has not arrived yet (``completed_at`` null) are only request-archived
    so an in-flight call is never raced.
    """
    from models import ApiRequestLog  # noqa: PLC0415

    if not enabled():
        return 0
    rows = (
        await db.exec(
            select(col(ApiRequestLog.id))
            .where(
                col(ApiRequestLog.id) > after_id,
                or_(
                    col(ApiRequestLog.request_object_key).is_(None)
                    & (
                        col(ApiRequestLog.system).is_not(None)
                        | col(ApiRequestLog.messages).is_not(None)
                        | col(ApiRequestLog.extra).is_not(None)
                    ),
                    col(ApiRequestLog.response_object_key).is_(None)
                    & col(ApiRequestLog.response).is_not(None),
                ),
            )
            .order_by(col(ApiRequestLog.id))
            .limit(batch)
        )
    ).all()
    last = 0
    for log_id in rows:
        await archive_quietly(db, log_id, REQUEST)
        await archive_quietly(db, log_id, RESPONSE)
        last = log_id
    return last


async def sweep_all(db_factory: Any, max_batches: int = 1000) -> int:
    """Run ``sweep_once`` until exhausted. Returns the number of batches processed."""
    after, batches = 0, 0
    while batches < max_batches:
        db = await db_factory()
        try:
            after = await sweep_once(db, after)
        finally:
            await db.close()
        if not after:
            break
        batches += 1
    return batches


async def sweeper_loop(db_factory: Any, interval: float = 300.0) -> None:
    """Background task: periodically retry failed uploads / backfill legacy rows."""
    while True:
        try:
            if enabled():
                await sweep_all(db_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("api_request_log sweeper failed")
        await asyncio.sleep(interval)


def status(row: Any, now: datetime | None = None) -> str:
    """inline | pending | archived | expired — derived from metadata only (no S3 I/O)."""
    now = now or datetime.now(UTC)
    has_inline = any(v is not None for v in (row.system, row.messages, row.extra, row.response))
    keyed = row.request_object_key or row.response_object_key
    if not keyed:
        return "pending" if enabled() and has_inline else "inline"
    if row.payload_expires_at is not None and row.payload_expires_at <= now:
        return "expired"
    return "archived"


def _get(key: str) -> bytes:
    return _s3().get_object(Bucket=bucket(), Key=key)["Body"].read()


async def read_payload(row: Any, now: datetime | None = None) -> dict[str, Any]:
    """Return the full payload (system/messages/extra/response) for one row.

    Falls back to inline columns for legacy/unarchived rows. Raises PayloadUnavailable with
    reason ``expired`` (past retention), ``missing`` (object absent before expiry) or
    ``error`` (S3 failure).
    """
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {
        "system": row.system,
        "messages": row.messages,
        "extra": row.extra,
        "response": row.response,
    }
    for key in (row.request_object_key, row.response_object_key):
        if not key:
            continue
        if row.payload_expires_at is not None and row.payload_expires_at <= now:
            raise PayloadUnavailable("expired")
        try:
            out.update(decode(await asyncio.to_thread(_get, key)))
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise PayloadUnavailable("missing") from exc
            logger.warning("api_request_log read %s failed: %s", key, exc)
            raise PayloadUnavailable("error") from exc
    return out
