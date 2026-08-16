"""Optional S3 sync: upload session-log dot-directories to an S3 bucket."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATHS = ["~/.codex", "~/.claude", "~/.pi"]


def _walk(root: Path) -> list[Path]:
    """Return all regular files under *root* recursively, excluding tmp/ subdirectories."""
    files: list[Path] = []
    for dirpath, dirs, filenames in os.walk(root):
        dirs[:] = [d for d in dirs if d != "tmp"]
        for name in filenames:
            files.append(Path(dirpath) / name)
    return files


def _md5_hex(path: Path) -> str:
    """Return the hex MD5 of a local file — matches S3 ETag for single-part uploads."""
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_signature(path: Path) -> tuple[int, int]:
    """Return fields that change when a file is modified or appended to."""
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _remote_etag_matches(s3, bucket: str, key: str, local_md5: str) -> bool:
    """Return True if S3 already has content with the given MD5 hash."""
    from botocore.exceptions import ClientError

    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise
    remote_etag = head["ETag"].strip('"')
    # Multi-part ETags contain a dash (e.g. "abc123-4") and can't be compared
    # to a plain MD5. Upload unconditionally in that case.
    if "-" in remote_etag:
        return False
    return remote_etag == local_md5


def _sync_one_file(s3, bucket: str, key: str, local_file: Path) -> str:
    """Upload one file if needed, but never treat an actively changing file as synced.

    Session logs are often appended while the periodic sync is running.  The old
    implementation could hash a half-written local file, skip because S3 matched
    that old hash, and then leave the later appended bytes behind until a future
    sync.  It could also upload a snapshot that became stale during upload.  We
    require the file's size/mtime to remain stable across hashing and upload;
    otherwise we retry briefly and leave it for the next sync.
    """
    for attempt in range(3):
        before = _file_signature(local_file)
        local_md5 = _md5_hex(local_file)
        after_hash = _file_signature(local_file)
        if before != after_hash:
            logger.debug("S3 sync: %s changed while hashing; retrying", local_file)
            time.sleep(0.1 * (attempt + 1))
            continue

        if _remote_etag_matches(s3, bucket, key, local_md5):
            return "skipped"

        s3.upload_file(str(local_file), bucket, key)
        after_upload = _file_signature(local_file)
        if before == after_upload:
            return "uploaded"

        logger.debug("S3 sync: %s changed during upload; retrying", local_file)
        time.sleep(0.1 * (attempt + 1))

    return "changed"


def _sync_paths_sync(*, bucket: str, prefix: str, paths: list[str]) -> None:
    """Blocking sync of *paths* to S3. Run via asyncio.to_thread."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    s3 = boto3.client("s3")
    hostname = socket.gethostname()

    for raw_path in paths:
        src = Path(os.path.expanduser(raw_path))
        if not src.exists():
            logger.debug("S3 sync: %s does not exist, skipping", src)
            continue

        # Path layout: <prefix>/<hostname>/<basename>/…
        # hostname disambiguates uploads from different worker machines.
        parts = [p for p in [prefix.rstrip("/"), hostname, src.name] if p]
        key_prefix = "/".join(parts)
        uploaded = skipped = changed = 0

        for local_file in _walk(src):
            rel = local_file.relative_to(src)
            s3_key = f"{key_prefix}/{rel.as_posix()}"
            try:
                result = _sync_one_file(s3, bucket, s3_key, local_file)
                if result == "uploaded":
                    uploaded += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    changed += 1
            except (OSError, BotoCoreError, ClientError) as exc:
                logger.warning("S3 upload failed for %s → %s: %s", local_file, s3_key, exc)

        logger.info(
            "S3 sync: %s → s3://%s/%s (uploaded=%d skipped=%d changed=%d)",
            src,
            bucket,
            key_prefix,
            uploaded,
            skipped,
            changed,
        )


async def sync_paths(*, bucket: str, prefix: str, paths: list[str]) -> None:
    """Async wrapper: sync each path to ``s3://<bucket>/<prefix>/<basename>``.

    Failures are logged and swallowed — sync must never affect task execution.
    """
    try:
        await asyncio.to_thread(_sync_paths_sync, bucket=bucket, prefix=prefix, paths=paths)
    except ImportError:
        logger.warning("boto3 not installed — S3 sync disabled. Run: pip install boto3")
    except Exception as exc:
        logger.warning("S3 sync error: %s", exc)
