"""Tests for pioneer_worker.s3_log_sync."""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, call, patch

import pytest
from pioneer_worker.s3_log_sync import S3LogSyncer, create_syncer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_syncer(tmp_path, bucket="test-bucket", prefix="logs", interval=1):
    """Return an S3LogSyncer with a mocked boto3 client."""
    with patch("pioneer_worker.s3_log_sync._BOTO3_AVAILABLE", True):
        with patch("pioneer_worker.s3_log_sync.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            syncer = S3LogSyncer(bucket=bucket, prefix=prefix, interval=interval)
    syncer._client = mock_client
    return syncer, mock_client


# ---------------------------------------------------------------------------
# S3LogSyncer unit tests
# ---------------------------------------------------------------------------


def test_sync_file_uploads_to_correct_key(tmp_path):
    syncer, mock_client = _make_syncer(tmp_path)
    log_file = tmp_path / "task.jsonl"
    log_file.write_text('{"type":"result"}\n')

    syncer.sync_file(str(log_file), "guild1/worker1/t-abc.jsonl")

    mock_client.upload_file.assert_called_once_with(
        str(log_file), "test-bucket", "logs/guild1/worker1/t-abc.jsonl"
    )


def test_sync_file_no_prefix(tmp_path):
    syncer, mock_client = _make_syncer(tmp_path, prefix="")
    log_file = tmp_path / "task.jsonl"
    log_file.write_text("{}\n")

    syncer.sync_file(str(log_file), "guild1/worker1/t-abc.jsonl")

    mock_client.upload_file.assert_called_once_with(
        str(log_file), "test-bucket", "guild1/worker1/t-abc.jsonl"
    )


def test_sync_file_missing_file_is_noop(tmp_path):
    syncer, mock_client = _make_syncer(tmp_path)

    syncer.sync_file(str(tmp_path / "nonexistent.jsonl"), "guild1/w/t.jsonl")

    mock_client.upload_file.assert_not_called()


def test_sync_file_upload_error_does_not_raise(tmp_path):
    syncer, mock_client = _make_syncer(tmp_path)
    mock_client.upload_file.side_effect = Exception("network error")
    log_file = tmp_path / "task.jsonl"
    log_file.write_text("{}\n")

    # Must not raise.
    syncer.sync_file(str(log_file), "guild1/w/t.jsonl")


def test_sync_all_uploads_registered_files(tmp_path):
    syncer, mock_client = _make_syncer(tmp_path)

    f1 = tmp_path / "a.jsonl"
    f1.write_text("{}\n")
    f2 = tmp_path / "b.jsonl"
    f2.write_text("{}\n")

    syncer.register(str(f1), "g/w/a.jsonl")
    syncer.register(str(f2), "g/w/b.jsonl")
    syncer.sync_all()

    assert mock_client.upload_file.call_count == 2


def test_unregister_stops_periodic_uploads(tmp_path):
    syncer, mock_client = _make_syncer(tmp_path)

    f1 = tmp_path / "a.jsonl"
    f1.write_text("{}\n")

    syncer.register(str(f1), "g/w/a.jsonl")
    syncer.unregister(str(f1))
    syncer.sync_all()

    mock_client.upload_file.assert_not_called()


def test_sync_file_noop_when_boto3_unavailable(tmp_path):
    with patch("pioneer_worker.s3_log_sync._BOTO3_AVAILABLE", False):
        syncer = S3LogSyncer(bucket="b", prefix="p", interval=60)
        syncer._client = None

    log_file = tmp_path / "task.jsonl"
    log_file.write_text("{}\n")

    # Must not raise even though _client is None.
    syncer.sync_file(str(log_file), "g/w/t.jsonl")


def test_start_stop_thread(tmp_path):
    syncer, _ = _make_syncer(tmp_path, interval=60)
    syncer.start()
    assert syncer._thread is not None
    assert syncer._thread.is_alive()
    thread = syncer._thread  # capture before stop() clears it
    syncer.stop()
    assert syncer._thread is None
    assert not thread.is_alive()


# ---------------------------------------------------------------------------
# create_syncer
# ---------------------------------------------------------------------------


def test_create_syncer_returns_none_when_no_bucket(monkeypatch):
    monkeypatch.delenv("LOG_S3_BUCKET", raising=False)
    result = create_syncer()
    assert result is None


def test_create_syncer_returns_none_when_boto3_missing(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    with patch("pioneer_worker.s3_log_sync._BOTO3_AVAILABLE", False):
        result = create_syncer()
    assert result is None


def test_create_syncer_returns_syncer_when_configured(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("LOG_S3_PREFIX", "pioneer/logs")
    monkeypatch.setenv("LOG_S3_SYNC_INTERVAL_SECONDS", "30")
    with patch("pioneer_worker.s3_log_sync._BOTO3_AVAILABLE", True):
        with patch("pioneer_worker.s3_log_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = MagicMock()
            syncer = create_syncer()
    assert syncer is not None
    assert syncer.bucket == "my-bucket"
    assert syncer.prefix == "pioneer/logs"
    assert syncer.interval == 30


def test_create_syncer_invalid_interval_defaults_to_60(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("LOG_S3_SYNC_INTERVAL_SECONDS", "not-a-number")
    with patch("pioneer_worker.s3_log_sync._BOTO3_AVAILABLE", True):
        with patch("pioneer_worker.s3_log_sync.boto3") as mock_boto3:
            mock_boto3.client.return_value = MagicMock()
            syncer = create_syncer()
    assert syncer is not None
    assert syncer.interval == 60
