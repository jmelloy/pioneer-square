"""Unit tests for pioneer_worker.s3_log_sync."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pioneer_worker.s3_log_sync import S3LogSync

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_boto3(client=None):
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = client or MagicMock()
    return mock_boto3


def _make_syncer(
    *, bucket="b", prefix="worker-logs", interval=60, storage_class="STANDARD_IA", s3_client=None
):
    return S3LogSync(
        bucket=bucket,
        prefix=prefix,
        interval=interval,
        storage_class=storage_class,
        s3_client=s3_client or MagicMock(),
    )


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


def test_from_env_returns_instance_when_configured(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("LOG_S3_PREFIX", "my-prefix")
    monkeypatch.setenv("LOG_S3_SYNC_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("LOG_S3_STORAGE_CLASS", "STANDARD")

    with patch("pioneer_worker.s3_log_sync._try_import_boto3", return_value=_make_mock_boto3()):
        syncer = S3LogSync.from_env()

    assert isinstance(syncer, S3LogSync)
    assert syncer._bucket == "my-bucket"
    assert syncer._prefix == "my-prefix"
    assert syncer._interval == 30
    assert syncer._storage_class == "STANDARD"


def test_from_env_uses_default_prefix_interval_and_storage_class(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.delenv("LOG_S3_PREFIX", raising=False)
    monkeypatch.delenv("LOG_S3_SYNC_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("LOG_S3_STORAGE_CLASS", raising=False)

    with patch("pioneer_worker.s3_log_sync._try_import_boto3", return_value=_make_mock_boto3()):
        syncer = S3LogSync.from_env()

    assert syncer is not None
    assert syncer._prefix == "worker-logs"
    assert syncer._interval == 60
    assert syncer._storage_class == "STANDARD_IA"


def test_from_env_invalid_interval_defaults_to_60(monkeypatch):
    monkeypatch.setenv("LOG_S3_BUCKET", "my-bucket")
    monkeypatch.setenv("LOG_S3_SYNC_INTERVAL_SECONDS", "not-a-number")

    with patch("pioneer_worker.s3_log_sync._try_import_boto3", return_value=_make_mock_boto3()):
        syncer = S3LogSync.from_env()

    assert syncer is not None
    assert syncer._interval == 60


# ---------------------------------------------------------------------------
# S3 key convention — final upload
# ---------------------------------------------------------------------------


def test_final_key_follows_convention(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("hello\n")

    syncer.start(log_file, guild_id="g-abc123", worker_id="w-xyz789", task_id="t-def456")
    syncer.finish()

    calls = mock_client.upload_file.call_args_list
    final_call = calls[-1]
    assert final_call.args[2] == "worker-logs/g-abc123/w-xyz789/t-def456/agent.log"


# ---------------------------------------------------------------------------
# Interim snapshot keys
# ---------------------------------------------------------------------------


def test_interim_key_uses_snapshot_path(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(interval=0.05, s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("hello\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    time.sleep(0.2)
    syncer.finish()

    calls = mock_client.upload_file.call_args_list
    assert len(calls) >= 2
    interim_keys = [c.args[2] for c in calls[:-1]]
    assert all("snapshots/" in k for k in interim_keys)
    # Snapshot keys should end with .log and contain a timestamp
    for k in interim_keys:
        assert k.startswith("worker-logs/g-1/w-1/t-1/snapshots/")
        assert k.endswith(".log")


def test_interim_key_is_unique_per_tick(tmp_path):
    """Two periodic ticks must produce different keys so uploads are not overwritten."""
    mock_client = MagicMock()
    syncer = _make_syncer(interval=0.05, s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    time.sleep(0.3)
    syncer.finish()

    calls = mock_client.upload_file.call_args_list
    # Collect interim keys (everything before the last final call)
    interim_keys = [c.args[2] for c in calls[:-1]]
    if len(interim_keys) >= 2:
        assert len(set(interim_keys)) == len(interim_keys), "interim keys must be unique"


# ---------------------------------------------------------------------------
# upload called on task completion (finish)
# ---------------------------------------------------------------------------


def test_finish_uploads_log_file(tmp_path):
    mock_client = MagicMock()
    syncer = S3LogSync(
        bucket="test-bucket",
        prefix="pfx",
        interval=3600,
        storage_class="STANDARD_IA",
        s3_client=mock_client,
    )

    log_file = tmp_path / "agent.log"
    log_file.write_text("task output\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    syncer.finish()

    assert mock_client.upload_file.call_count == 1
    args, kwargs = mock_client.upload_file.call_args
    assert args[0] == str(log_file)
    assert args[1] == "test-bucket"
    assert args[2] == "pfx/g-1/w-1/t-1/agent.log"
    assert kwargs["ExtraArgs"]["Metadata"]["upload-type"] == "final"


def test_finish_with_no_start_is_noop():
    mock_client = MagicMock()
    syncer = _make_syncer(s3_client=mock_client)
    syncer.finish()
    mock_client.upload_file.assert_not_called()


def test_upload_skipped_when_file_missing(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(s3_client=mock_client)
    missing = tmp_path / "nonexistent.log"

    syncer.start(missing, guild_id="g-1", worker_id="w-1", task_id="t-1")
    syncer.finish()

    mock_client.upload_file.assert_not_called()


def test_upload_failure_is_swallowed(tmp_path, caplog):
    mock_client = MagicMock()
    mock_client.upload_file.side_effect = RuntimeError("network error")

    syncer = _make_syncer(s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    import logging

    with caplog.at_level(logging.WARNING):
        syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
        syncer.finish()

    assert any("upload failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Object metadata
# ---------------------------------------------------------------------------


def test_upload_includes_required_metadata(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-abc", worker_id="w-xyz", task_id="t-123")
    syncer.finish()

    _, kwargs = mock_client.upload_file.call_args
    meta = kwargs["ExtraArgs"]["Metadata"]
    assert meta["task-id"] == "t-123"
    assert meta["worker-id"] == "w-xyz"
    assert meta["guild-id"] == "g-abc"
    assert "upload-time" in meta
    assert meta["upload-type"] == "final"


def test_interim_upload_metadata_type(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(interval=0.05, s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    time.sleep(0.2)
    syncer.finish()

    calls = mock_client.upload_file.call_args_list
    assert len(calls) >= 2
    for c in calls[:-1]:
        assert c.kwargs["ExtraArgs"]["Metadata"]["upload-type"] == "interim"
    assert calls[-1].kwargs["ExtraArgs"]["Metadata"]["upload-type"] == "final"


# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------


def test_upload_uses_default_storage_class_standard_ia(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(storage_class="STANDARD_IA", s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    syncer.finish()

    _, kwargs = mock_client.upload_file.call_args
    assert kwargs["ExtraArgs"]["StorageClass"] == "STANDARD_IA"


def test_upload_uses_custom_storage_class(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(storage_class="GLACIER", s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    syncer.finish()

    _, kwargs = mock_client.upload_file.call_args
    assert kwargs["ExtraArgs"]["StorageClass"] == "GLACIER"


# ---------------------------------------------------------------------------
# Content type
# ---------------------------------------------------------------------------


def test_upload_sets_content_type_text_plain(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    syncer.finish()

    _, kwargs = mock_client.upload_file.call_args
    assert kwargs["ExtraArgs"]["ContentType"] == "text/plain"


# ---------------------------------------------------------------------------
# Periodic sync
# ---------------------------------------------------------------------------


def test_periodic_sync_calls_upload(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(interval=0.05, s3_client=mock_client)

    log_file = tmp_path / "agent.log"
    log_file.write_text("line 1\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    time.sleep(0.2)
    syncer.finish()

    assert mock_client.upload_file.call_count >= 2


def test_background_thread_is_daemon(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    assert syncer._thread is not None
    assert syncer._thread.daemon is True
    syncer.finish()


def test_finish_stops_background_thread(tmp_path):
    mock_client = MagicMock()
    syncer = _make_syncer(s3_client=mock_client)
    log_file = tmp_path / "agent.log"
    log_file.write_text("data\n")

    syncer.start(log_file, guild_id="g-1", worker_id="w-1", task_id="t-1")
    thread = syncer._thread
    syncer.finish()

    assert syncer._thread is None
    assert not thread.is_alive()
