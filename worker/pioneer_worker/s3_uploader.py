"""Optional S3 sync: upload session-log dot-directories to an S3 bucket."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATHS = ["~/.codex", "~/.claude", "~/.pi"]


def _walk(root: Path) -> list[Path]:
    """Return all regular files under *root* recursively."""
    files: list[Path] = []
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            files.append(Path(dirpath) / name)
    return files


def _sync_paths_sync(*, bucket: str, prefix: str, paths: list[str]) -> None:
    """Blocking sync of *paths* to S3. Run via asyncio.to_thread."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    s3 = boto3.client("s3")

    for raw_path in paths:
        src = Path(os.path.expanduser(raw_path))
        if not src.exists():
            logger.debug("S3 sync: %s does not exist, skipping", src)
            continue

        key_prefix = f"{prefix.rstrip('/')}/{src.name}" if prefix else src.name
        uploaded = 0

        for local_file in _walk(src):
            rel = local_file.relative_to(src)
            s3_key = f"{key_prefix}/{rel.as_posix()}"
            try:
                s3.upload_file(str(local_file), bucket, s3_key)
                uploaded += 1
            except (BotoCoreError, ClientError) as exc:
                logger.warning("S3 upload failed for %s → %s: %s", local_file, s3_key, exc)

        logger.info("S3 sync: %s → s3://%s/%s (%d file(s))", src, bucket, key_prefix, uploaded)


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
