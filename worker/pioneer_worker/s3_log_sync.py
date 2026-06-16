"""Optional S3 sync for worker task session logs.

Enabled when the ``LOG_S3_BUCKET`` environment variable is set. Requires
``boto3``; if it is not installed, a warning is logged and the feature is
silently disabled — the worker continues normally.

Environment variables
---------------------
LOG_S3_BUCKET
    S3 bucket name. If absent, the feature is disabled entirely.
LOG_S3_PREFIX
    Optional key prefix (e.g. ``"pioneer/logs"``). Defaults to ``""``.
LOG_S3_SYNC_INTERVAL_SECONDS
    How often the background thread uploads registered log files.  Default 60.

Credentials are resolved via the standard AWS credential chain (environment
variables, ``~/.aws/credentials``, instance metadata, etc.).
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

try:
    import boto3
    import botocore.exceptions

    _BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None  # type: ignore[assignment]
    botocore = None  # type: ignore[assignment]
    _BOTO3_AVAILABLE = False
    logger.warning(
        "boto3 not installed — S3 log sync will be disabled even if LOG_S3_BUCKET is set"
    )


class S3LogSyncer:
    """Periodically uploads task log files to an S3 bucket.

    Usage::

        syncer = S3LogSyncer(bucket="my-bucket", prefix="pioneer/logs", interval=60)
        syncer.start()

        # Register a file for periodic sync.
        syncer.register("path/to/task-t-abc123.jsonl", "guild123/w-xyz/t-abc123.jsonl")

        # Sync immediately (e.g. at task completion).
        syncer.sync_file("path/to/task-t-abc123.jsonl", "guild123/w-xyz/t-abc123.jsonl")

        # On shutdown.
        syncer.stop()
    """

    def __init__(self, bucket: str, prefix: str, interval: int = 60) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.interval = interval

        self._files: dict[str, str] = {}  # local_path -> s3_key
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = boto3.client("s3") if _BOTO3_AVAILABLE else None  # type: ignore[union-attr]

    def _full_key(self, s3_key: str) -> str:
        return f"{self.prefix}/{s3_key}" if self.prefix else s3_key

    def register(self, local_path: str, s3_key: str) -> None:
        """Register *local_path* for periodic sync to *s3_key* under the configured prefix."""
        with self._lock:
            self._files[local_path] = s3_key

    def unregister(self, local_path: str) -> None:
        """Remove a file from periodic sync (does not delete the S3 object)."""
        with self._lock:
            self._files.pop(local_path, None)

    def sync_file(self, local_path: str, s3_key: str) -> None:
        """Upload *local_path* to S3 immediately.

        No-op if the file does not exist yet. Errors are logged as warnings and
        swallowed — a sync failure must never affect task outcome.
        """
        if not _BOTO3_AVAILABLE or self._client is None:
            return
        if not os.path.isfile(local_path):
            return
        full_key = self._full_key(s3_key)
        try:
            self._client.upload_file(local_path, self.bucket, full_key)
            logger.debug("S3 sync: uploaded %s -> s3://%s/%s", local_path, self.bucket, full_key)
        except Exception as exc:
            logger.warning(
                "S3 upload failed for %s -> s3://%s/%s: %s",
                local_path,
                self.bucket,
                full_key,
                exc,
            )

    def sync_all(self) -> None:
        """Upload all registered files. Called by the background thread."""
        with self._lock:
            items = list(self._files.items())
        for local_path, s3_key in items:
            self.sync_file(local_path, s3_key)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval):
            self.sync_all()

    def start(self) -> None:
        """Start the background sync thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="s3-log-sync")
        self._thread.start()
        logger.info(
            "S3 log syncer started (bucket=%s prefix=%r interval=%ds)",
            self.bucket,
            self.prefix,
            self.interval,
        )

    def stop(self) -> None:
        """Stop the background thread and wait for it to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def create_syncer() -> S3LogSyncer | None:
    """Build an :class:`S3LogSyncer` from environment variables.

    Returns ``None`` if ``LOG_S3_BUCKET`` is not set or ``boto3`` is unavailable.
    """
    bucket = os.environ.get("LOG_S3_BUCKET", "").strip()
    if not bucket:
        return None

    if not _BOTO3_AVAILABLE:
        logger.warning(
            "LOG_S3_BUCKET=%r is set but boto3 is not installed — S3 log sync disabled",
            bucket,
        )
        return None

    prefix = os.environ.get("LOG_S3_PREFIX", "").strip()
    try:
        interval = int(os.environ.get("LOG_S3_SYNC_INTERVAL_SECONDS", "60"))
    except ValueError:
        logger.warning("Invalid LOG_S3_SYNC_INTERVAL_SECONDS — using default 60s")
        interval = 60

    logger.info(
        "S3 log sync enabled: bucket=%s prefix=%r interval=%ds",
        bucket,
        prefix,
        interval,
    )
    return S3LogSyncer(bucket=bucket, prefix=prefix, interval=interval)
