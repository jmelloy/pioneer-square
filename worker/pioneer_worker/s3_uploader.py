"""Optional S3 sync: upload session-log dot-directories to an S3 bucket."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
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


def _already_uploaded(s3, bucket: str, key: str, local_file: Path) -> bool:
    """Return True if S3 already has this file with the same content (ETag == MD5)."""
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
    return remote_etag == _md5_hex(local_file)


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
        uploaded = skipped = 0

        for local_file in _walk(src):
            rel = local_file.relative_to(src)
            s3_key = f"{key_prefix}/{rel.as_posix()}"
            try:
                if _already_uploaded(s3, bucket, s3_key, local_file):
                    skipped += 1
                    continue
                s3.upload_file(str(local_file), bucket, s3_key)
                uploaded += 1
            except (BotoCoreError, ClientError) as exc:
                logger.warning("S3 upload failed for %s → %s: %s", local_file, s3_key, exc)

        logger.info(
            "S3 sync: %s → s3://%s/%s (uploaded=%d skipped=%d)",
            src,
            bucket,
            key_prefix,
            uploaded,
            skipped,
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
