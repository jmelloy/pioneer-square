"""Optional S3 log sync for worker task logs.

Controlled by environment variables:
  LOG_S3_BUCKET                — bucket name; feature disabled if unset
  LOG_S3_PREFIX                — key prefix (default: "worker-logs")
  LOG_S3_SYNC_INTERVAL_SECONDS — periodic sync interval in seconds (default: 60)
  LOG_S3_STORAGE_CLASS         — S3 storage class (default: "STANDARD_IA")

S3 key layout:
  Final:   {prefix}/{guild_id}/{worker_id}/{task_id}/agent.log
  Interim: {prefix}/{guild_id}/{worker_id}/{task_id}/snapshots/{iso_timestamp}.log

Each upload carries object metadata for auditability:
  x-amz-meta-task-id, worker-id, guild-id, upload-time, upload-type

Credentials use the standard AWS credential chain (env vars → ~/.aws → IMDS).
If boto3 is not installed and LOG_S3_BUCKET is set, a warning is logged and
the feature is disabled rather than crashing.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

UploadType = Literal["interim", "final"]


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

    Interim uploads go to timestamped snapshot keys so every upload is preserved.
    The final upload goes to the canonical agent.log key.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        interval: int,
        storage_class: str,
        s3_client,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._interval = interval
        self._storage_class = storage_class
        self._client = s3_client
        self._log_path: Path | None = None
        self._s3_key_base: str | None = None
        self._guild_id: str | None = None
        self._worker_id: str | None = None
        self._task_id: str | None = None
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
        storage_class = os.environ.get("LOG_S3_STORAGE_CLASS", "STANDARD_IA")
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

        return cls(
            bucket=bucket,
            prefix=prefix,
            interval=interval,
            storage_class=storage_class,
            s3_client=client,
        )

    def start(self, log_path: str | Path, *, guild_id: str, worker_id: str, task_id: str) -> None:
        """Start the periodic sync background thread for *log_path*."""
        self._log_path = Path(log_path)
        self._guild_id = guild_id
        self._worker_id = worker_id
        self._task_id = task_id
        self._s3_key_base = f"{self._prefix}/{guild_id}/{worker_id}/{task_id}"
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._sync_loop, daemon=True, name=f"s3-sync-{task_id}"
        )
        self._thread.start()
        logger.info(
            "S3 log sync started: s3://%s/%s/ (interval=%ds, storage_class=%s)",
            self._bucket,
            self._s3_key_base,
            self._interval,
            self._storage_class,
        )

    def finish(self) -> None:
        """Stop the periodic sync thread and perform a final upload."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=15.0)
        self._thread = None
        self._upload("final")
        logger.info("S3 log sync finished: s3://%s/%s/agent.log", self._bucket, self._s3_key_base)

    def _upload(self, upload_type: UploadType) -> None:
        if self._log_path is None or self._s3_key_base is None:
            return
        if not self._log_path.exists():
            return

        now = datetime.now(UTC)
        iso_ts = now.strftime("%Y%m%dT%H%M%S.%fZ")

        if upload_type == "final":
            key = f"{self._s3_key_base}/agent.log"
        else:
            key = f"{self._s3_key_base}/snapshots/{iso_ts}.log"

        extra_args = {
            "Metadata": {
                "task-id": self._task_id or "",
                "worker-id": self._worker_id or "",
                "guild-id": self._guild_id or "",
                "upload-time": now.isoformat(),
                "upload-type": upload_type,
            },
            "StorageClass": self._storage_class,
            "ContentType": "text/plain",
        }

        try:
            self._client.upload_file(str(self._log_path), self._bucket, key, ExtraArgs=extra_args)
            logger.debug("Uploaded log to s3://%s/%s (%s)", self._bucket, key, upload_type)
        except Exception as exc:
            logger.warning("S3 log upload failed for %s: %s", key, exc)

    def _sync_loop(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            self._upload("interim")
