"""Optional S3 log sync for worker task logs.

Controlled by environment variables:
  LOG_S3_BUCKET                — bucket name; feature disabled if unset
  LOG_S3_PREFIX                — key prefix (default: "worker-logs")
  LOG_S3_SYNC_INTERVAL_SECONDS — periodic sync interval in seconds (default: 60)

S3 key layout: {prefix}/{guild_id}/{worker_id}/{task_id}/agent.log

Credentials use the standard AWS credential chain (env vars → ~/.aws → IMDS).
If boto3 is not installed and LOG_S3_BUCKET is set, a warning is logged and
the feature is disabled rather than crashing.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def _try_import_boto3():
    try:
        import boto3  # type: ignore[import-untyped]

        return boto3
    except ImportError:
        return None


class S3LogSync:
    """Periodically syncs a task log file to S3 while a task is running.

    One instance handles one task; create a fresh one per task via ``from_env()``.

    Lifecycle::

        syncer = S3LogSync.from_env()
        if syncer:
            syncer.start(log_path, guild_id="g-abc", worker_id="w-xyz", task_id="t-123")
            # ... task runs ...
            syncer.finish()   # stop background thread then do final upload
    """

    def __init__(self, *, bucket: str, prefix: str, interval: int, s3_client) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._interval = interval
        self._client = s3_client
        self._log_path: Path | None = None
        self._s3_key: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls) -> S3LogSync | None:
        """Build an instance from environment variables, or return None if disabled.

        Returns None silently when LOG_S3_BUCKET is unset (zero runtime cost).
        Logs a warning and returns None when boto3 is missing.
        """
        bucket = os.environ.get("LOG_S3_BUCKET")
        if not bucket:
            return None

        boto3 = _try_import_boto3()
        if boto3 is None:
            logger.warning(
                "LOG_S3_BUCKET is set but boto3 is not installed — S3 log sync disabled. "
                "Install it with: pip install boto3"
            )
            return None

        prefix = os.environ.get("LOG_S3_PREFIX", "worker-logs")
        try:
            interval = int(os.environ.get("LOG_S3_SYNC_INTERVAL_SECONDS", "60"))
        except ValueError:
            logger.warning("Invalid LOG_S3_SYNC_INTERVAL_SECONDS; using default of 60s")
            interval = 60

        try:
            client = boto3.client("s3")
        except Exception as exc:
            logger.warning("Failed to create S3 client — S3 log sync disabled: %s", exc)
            return None

        return cls(bucket=bucket, prefix=prefix, interval=interval, s3_client=client)

    def start(self, log_path: str | Path, *, guild_id: str, worker_id: str, task_id: str) -> None:
        """Start the periodic sync background thread for *log_path*."""
        self._log_path = Path(log_path)
        self._s3_key = f"{self._prefix}/{guild_id}/{worker_id}/{task_id}/agent.log"
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._sync_loop, daemon=True, name=f"s3-sync-{task_id}"
        )
        self._thread.start()
        logger.info(
            "S3 log sync started: s3://%s/%s (interval=%ds)",
            self._bucket,
            self._s3_key,
            self._interval,
        )

    def finish(self) -> None:
        """Stop the periodic sync thread and perform a final upload."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=15.0)
        self._thread = None
        self._upload()
        logger.info("S3 log sync finished: s3://%s/%s", self._bucket, self._s3_key)

    def _upload(self) -> None:
        if self._log_path is None or self._s3_key is None:
            return
        if not self._log_path.exists():
            return
        try:
            self._client.upload_file(str(self._log_path), self._bucket, self._s3_key)
            logger.debug("Uploaded log to s3://%s/%s", self._bucket, self._s3_key)
        except Exception as exc:
            logger.warning("S3 log upload failed for %s: %s", self._s3_key, exc)

    def _sync_loop(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            self._upload()
