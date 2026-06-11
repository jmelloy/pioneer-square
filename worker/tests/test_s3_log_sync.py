"""Unit tests for pioneer_worker.s3_log_sync."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pioneer_worker.s3_log_sync import S3LogSync

# ---------------------------------------------------------------------------
# from_env — disabled paths
# ---------------------------------------------------------------------------


def test_from_env_no_bucket_returns_none(monkeypatch):
    monkeypatch.delenv("LOG_S3_BUCKET", raising=False)
    assert S3LogSync.from_env() is None


def test_from_env_no_bucket_is_silent(monkeypatch, caplog):
    monkeypatch.delenv("LOG_S3_BUCKET", raising=False)
    import logging

    with caplog.at_level(logging.WARNING):
        S3LogSync.from_env()
    assert not caplog.records


def test_from_env_boto3_missing_warns_and_returns_none(monkeypatch, caplog):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    import logging

    with (
        patch(
            "pioneer_worker.s3_log_sync._try_import_boto3",
            return_value=None,
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = S3LogSync.from_env()

    assert result is None
    assert any("boto3" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# from_env — happy path
# ---------------------------------------------------------------------------


def _make_mock_boto3(client=None):
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = client or MagicMock()
    return mock_boto3


def test_from_env_returns_instance_when_configured(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("LOG_S3_PREFIX", "my-prefix")
    monkeypatch.setenv("LOG_S3_SYNC_INTERVAL_SECONDS", "30")
    monkeypatch.delenv("LOG_S3_BUCKET", raising=False)  # reset
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")

    with patch("pioneer_worker.s3_log_sync._try_import_boto3", return_value=_make_mock_boto3()):
        syncer = S3LogSync.from_env()

    assert isinstance(syncer, S3LogSync)
    assert syncer._bucket == "my-bucket"
    assert syncer._prefix == "my-prefix"
    assert syncer._interval == 30


def test_from_env_uses_default_prefix_and_interval(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.delenv("LOG_S3_PREFIX", raising=False)
    monkeypatch.delenv("LOG_S3_SYNC_INTERVAL_SECONDS", raising=False)

    with patch("pioneer_worker.s3_log_sync._try_import_boto3", return_value=_make_mock_boto3()):
        syncer = S3LogSync.from_env()

    assert syncer is not None
    assert syncer._prefix == "worker-logs"
    assert syncer._interval == 60


def test_from_env_invalid_interval_defaults_to_60(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("LOG_S3_SYNC_INTERVAL_SECONDS", "not-a-number")

    with patch("pioneer_worker.s3_log_sync._try_import_boto3", return_value=_make_mock_boto3()):
        syncer = S3LogSync.from_env()

    assert syncer is not None
    assert syncer._interval == 60


# ---------------------------------------------------------------------------
# S3 key convention
# ---------------------------------------------------------------------------


def test_s3_key_follows_convention(tmp_path):
    mock_client = MagicMock()
    syncer = S3LogSync(bucket="b", prefix="worker-logs", interval=60, s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("hello\n")

    syncer.start(log_file, guild_id="g-abc123", worker_id="w-xyz789", task_id="t-def456")
    syncer.finish()

    expected_key = "worker-logs/g-abc123/w-xyz789/t-def456/agent.log"
    mock_client.upload_file.assert_called_with(str(log_file), "b", expected_key)


# ---------------------------------------------------------------------------
# upload called on task completion (finish)
# ---------------------------------------------------------------------------


def test_finish_uploads_log_file(tmp_path):
    mock_client = MagicMock()
    syncer = S3LogSync(bucket="test-bucket", prefix="pfx", interval=3600, s3_client=mock_client)

    log_file = tmp_path / "agent.log"
    log_file.write_text("task output\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    syncer.finish()

    mock_client.upload_file.assert_called_once_with(
        str(log_file), "test-bucket", "pfx/g-1/w-1/t-1/agent.log"
    )


def test_finish_with_no_start_is_noop():
    mock_client = MagicMock()
    syncer = S3LogSync(bucket="b", prefix="p", interval=60, s3_client=mock_client)
    syncer.finish()  # should not raise
    mock_client.upload_file.assert_not_called()


def test_upload_skipped_when_file_missing(tmp_path):
    mock_client = MagicMock()
    syncer = S3LogSync(bucket="b", prefix="p", interval=60, s3_client=mock_client)
    missing = tmp_path / "nonexistent.log"

    syncer.start(missing, guild_id="g-1", worker_id="w-1", task_id="t-1")
    syncer.finish()

    mock_client.upload_file.assert_not_called()


def test_upload_failure_is_swallowed(tmp_path, caplog):
    mock_client = MagicMock()
    mock_client.upload_file.side_effect = RuntimeError("network error")

    syncer = S3LogSync(bucket="b", prefix="p", interval=60, s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    import logging

    with caplog.at_level(logging.WARNING):
        syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
        syncer.finish()  # must not raise

    assert any("upload failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Periodic sync
# ---------------------------------------------------------------------------


def test_periodic_sync_calls_upload(tmp_path):
    mock_client = MagicMock()
    # Very short interval so the test doesn't take long
    syncer = S3LogSync(bucket="b", prefix="p", interval=0.05, s3_client=mock_client)

    log_file = tmp_path / "agent.log"
    log_file.write_text("line 1\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    time.sleep(0.2)  # allow at least one periodic tick
    syncer.finish()

    # upload_file should have been called at least twice: once periodic + once final
    assert mock_client.upload_file.call_count >= 2


def test_background_thread_is_daemon(tmp_path):
    mock_client = MagicMock()
    syncer = S3LogSync(bucket="b", prefix="p", interval=60, s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    assert syncer._thread is not None
    assert syncer._thread.daemon is True
    syncer.finish()


def test_finish_stops_background_thread(tmp_path):
    mock_client = MagicMock()
    syncer = S3LogSync(bucket="b", prefix="p", interval=60, s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    thread = syncer._thread
    syncer.finish()

    assert syncer._thread is None
    assert not thread.is_alive()
