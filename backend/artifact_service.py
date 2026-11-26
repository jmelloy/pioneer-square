"""S3-backed artifact storage for screenshots, images, HTML reports, text and PDFs.

Configuration (env vars; credentials come from the standard boto3 chain, i.e. an
IAM role in ECS or ``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` locally):

- ``PIONEER_ARTIFACT_BUCKET``   bucket name (falls back to ``PIONEER_S3_BUCKET``)
- ``PIONEER_ARTIFACT_PREFIX``   key prefix (default ``artifacts``)
- ``PIONEER_ARTIFACT_REGION``   optional region override
- ``PIONEER_ARTIFACT_PUBLIC_BASE_URL``  if set (e.g. a CloudFront domain), ``build_url`` returns
  ``<base>/<key>``; otherwise a pre-signed GET URL (internal use only).

Clients get ``public_url(id)``: a permanent backend path that streams the object from S3, so
links never expire and never expose bucket details.
- ``PIONEER_ARTIFACT_MAX_BYTES``  size limit (default 10 MiB)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass

MAX_ARTIFACT_BYTES_DEFAULT = 10 * 1024 * 1024
PRESIGN_EXPIRY_SECONDS = 7 * 24 * 3600

# content type -> (extension, artifact type). Binary formats are checked by magic bytes so
# the declared type can't lie; text formats must be valid UTF-8.
ALLOWED_TYPES = {
    "image/png": ("png", "image"),
    "image/jpeg": ("jpg", "image"),
    "image/webp": ("webp", "image"),
    "image/gif": ("gif", "image"),
    "application/pdf": ("pdf", "pdf"),
    "text/html": ("html", "html"),
    "text/plain": ("txt", "text"),
    "text/markdown": ("md", "text"),
    "text/csv": ("csv", "text"),
    "application/json": ("json", "text"),
}
ARTIFACT_TYPES = {"image", "html", "text", "pdf"}
_ALIASES = {"image/jpg": "image/jpeg", "text/x-markdown": "text/markdown"}


def artifact_type(content_type: str) -> str:
    return ALLOWED_TYPES[content_type][1]


class ArtifactError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ArtifactConfig:
    bucket: str
    prefix: str
    region: str | None
    public_base_url: str | None
    max_bytes: int


def load_config() -> ArtifactConfig | None:
    bucket = os.environ.get("PIONEER_ARTIFACT_BUCKET") or os.environ.get("PIONEER_S3_BUCKET")
    if not bucket:
        return None
    base = os.environ.get("PIONEER_ARTIFACT_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return ArtifactConfig(
        bucket=bucket,
        prefix=os.environ.get("PIONEER_ARTIFACT_PREFIX", "artifacts").strip("/"),
        region=os.environ.get("PIONEER_ARTIFACT_REGION") or None,
        public_base_url=base or None,
        max_bytes=int(os.environ.get("PIONEER_ARTIFACT_MAX_BYTES", MAX_ARTIFACT_BYTES_DEFAULT)),
    )


def sniff_content_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _is_text(data: bytes) -> bool:
    try:
        return "\x00" not in data.decode("utf-8")
    except UnicodeDecodeError:
        return False


def validate(data: bytes, declared_type: str | None, max_bytes: int) -> str:
    """Return the canonical content type or raise ``ArtifactError``."""
    if not data:
        raise ArtifactError("Empty file")
    if len(data) > max_bytes:
        raise ArtifactError(f"File exceeds {max_bytes} bytes", 413)
    declared = (declared_type or "").split(";")[0].strip().lower()
    declared = _ALIASES.get(declared, declared)
    if declared not in ALLOWED_TYPES:
        raise ArtifactError(
            "Unsupported type; allowed: PNG, JPG, WebP, GIF, PDF, HTML, TXT, MD, CSV, JSON", 415
        )
    if declared.startswith("text/") or declared == "application/json":
        if not _is_text(data):
            raise ArtifactError("Text artifacts must be UTF-8 without NUL bytes", 415)
    elif sniff_content_type(data) != declared:
        raise ArtifactError("File contents do not match declared content type", 415)
    return declared


_client = None


def _s3(cfg: ArtifactConfig):
    global _client
    if _client is None:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415

        # Default config presigns SigV2 against the global s3.amazonaws.com endpoint, which
        # S3 redirects (301/307) for buckets outside us-east-1, breaking direct image loads.
        # SigV4 + virtual addressing yields a stable regional URL: <bucket>.s3.<region>...
        _client = boto3.client(
            "s3",
            region_name=cfg.region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _client


def public_url(artifact_id: str) -> str:
    """Permanent, backend-relative URL served by ``GET /api/artifacts/{id}/public``."""
    return f"/api/artifacts/{artifact_id}/public"


def absolute_public_url(artifact_id: str, fallback_origin: str = "http://localhost:5173") -> str:
    """Public origin for links shared outside the app, including worker-generated messages."""
    base = (os.environ.get("FRONTEND_URL") or fallback_origin).rstrip("/")
    return base + public_url(artifact_id)


def open_object(key: str):
    """Return the S3 ``StreamingBody`` for ``key``; raise ``ArtifactError`` if unavailable."""
    cfg = load_config()
    if cfg is None:
        raise ArtifactError("Artifact storage is not configured", 503)
    try:
        return _s3(cfg).get_object(Bucket=cfg.bucket, Key=key)["Body"]
    except Exception as exc:  # botocore NoSuchKey / ClientError
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            raise ArtifactError("Artifact not found", 404) from exc
        raise ArtifactError(f"Download failed: {exc}", 502) from exc


def build_url(cfg: ArtifactConfig, key: str) -> str:
    if cfg.public_base_url:
        return f"{cfg.public_base_url}/{key}"
    return _s3(cfg).generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg.bucket, "Key": key},
        ExpiresIn=PRESIGN_EXPIRY_SECONDS,
    )


@dataclass
class StoredObject:
    id: str
    key: str
    url: str
    content_type: str
    size_bytes: int
    type: str = "image"


async def store(
    data: bytes, declared_type: str | None, guild_slug: str, task_id: str | None
) -> StoredObject:
    cfg = load_config()
    if cfg is None:
        raise ArtifactError("Artifact storage is not configured", 503)
    content_type = validate(data, declared_type, cfg.max_bytes)
    artifact_id = str(uuid.uuid4())
    parts = [p for p in (cfg.prefix, guild_slug, task_id or "misc") if p]
    key = "/".join(parts) + f"/{artifact_id}.{ALLOWED_TYPES[content_type][0]}"

    def _put() -> None:
        _s3(cfg).put_object(
            Bucket=cfg.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="public, max-age=31536000, immutable",
        )

    try:
        await asyncio.to_thread(_put)
    except Exception as exc:  # botocore errors
        raise ArtifactError(f"Upload failed: {exc}", 502) from exc
    return StoredObject(
        artifact_id,
        key,
        public_url(artifact_id),
        content_type,
        len(data),
        artifact_type(content_type),
    )
