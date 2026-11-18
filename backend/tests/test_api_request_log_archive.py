"""Archival of api_request_log payloads to private S3 (issue #1487).

Uses an in-memory fake S3 and a fake session, so no Postgres or AWS is needed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from foreman import audit_payloads as ap  # noqa: E402
from models import ApiRequestLog  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, fail_puts: int = 0):
        self.objects: dict[str, bytes] = {}
        self.fail_puts = fail_puts
        self.put_calls = 0
        self.get_calls = 0
        self.put_kwargs: list[dict] = []

    def put_object(self, **kw):
        self.put_calls += 1
        if self.fail_puts > 0:
            self.fail_puts -= 1
            raise OSError("s3 down")
        self.put_kwargs.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise NoSuchKey()
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):  # noqa: N803
        self.get_calls += 1
        if Key not in self.objects:
            raise NoSuchKey()
        data = self.objects[Key]
        return {"Body": SimpleNamespace(read=lambda: data)}


def _row(**kw) -> SimpleNamespace:
    base = dict(
        id=1,
        created_at=datetime(2026, 11, 17, 12, tzinfo=UTC),
        completed_at=None,
        request_id=None,
        model="m",
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
        extra={"max_tokens": 10},
        response=None,
        request_object_key=None,
        response_object_key=None,
        payload_archived_at=None,
        payload_expires_at=None,
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=0,
        cache_write_tokens=0,
        stop_reason=None,
        task_id=None,
        user_id="u1",
        trigger=None,
        guild_id=7,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeDB:
    """Serves SELECT ApiRequestLog by id and applies UPDATE .values() to the row."""

    def __init__(self, *rows):
        self.rows = {r.id: r for r in rows}
        self.selects = 0
        self.commits = 0

    async def exec(self, stmt):
        if getattr(stmt, "is_update", False):
            params = stmt.compile().params
            row = self.rows[params["id_1"]]
            for k, v in params.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            return None
        self.selects += 1
        rows = list(self.rows.values())
        return SimpleNamespace(
            one_or_none=lambda: rows[0] if rows else None, all=lambda: [r.id for r in rows]
        )

    async def commit(self):
        self.commits += 1

    async def close(self):
        pass


@pytest.fixture()
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("PIONEER_API_LOG_BUCKET", "bkt")
    monkeypatch.setattr(ap, "_s3", lambda: fake)
    monkeypatch.setattr(ap, "UPLOAD_BACKOFF_SECONDS", 0)
    return fake


# --- archival -----------------------------------------------------------------


def test_request_archived_compressed_and_inline_cleared(s3):
    row = _row()
    assert _run(ap.archive(FakeDB(row), 1, ap.REQUEST)) is True
    assert row.request_object_key == "api-request-log/2026/11/17/1/request.json.gz"
    assert (row.system, row.messages, row.extra) == (None, None, None)
    body = s3.objects[row.request_object_key]
    assert body[:2] == b"\x1f\x8b"  # gzip
    assert ap.decode(body)["messages"] == [{"role": "user", "content": "hi"}]
    assert s3.put_kwargs[0]["ServerSideEncryption"] == "AES256"
    assert row.payload_expires_at - row.payload_archived_at == timedelta(days=7)


def test_response_archived_and_metadata_kept(s3):
    row = _row(response={"content": [1]}, request_object_key="k", input_tokens=5)
    _run(ap.archive(FakeDB(row), 1, ap.RESPONSE))
    assert row.response is None and row.response_object_key.endswith("/response.json.gz")
    assert row.input_tokens == 5 and row.user_id == "u1"
    assert ap.decode(s3.objects[row.response_object_key]) == {"response": {"content": [1]}}


def test_disabled_without_bucket_keeps_inline(monkeypatch):
    monkeypatch.delenv("PIONEER_API_LOG_BUCKET", raising=False)
    row = _row()
    assert _run(ap.archive(FakeDB(row), 1, ap.REQUEST)) is False
    assert row.request_object_key is None and row.messages is not None
    assert ap.status(row) == "inline"


def test_archive_is_idempotent(s3):
    row, db = _row(), None
    db = FakeDB(row)
    _run(ap.archive(db, 1, ap.REQUEST))
    puts = s3.put_calls
    assert _run(ap.archive(db, 1, ap.REQUEST)) is True
    assert s3.put_calls == puts


# --- upload failure / retry ---------------------------------------------------


def test_transient_failure_is_retried(s3):
    s3.fail_puts = 2
    row = _row()
    assert _run(ap.archive(FakeDB(row), 1, ap.REQUEST)) is True
    assert s3.put_calls == 3 and row.request_object_key


def test_persistent_failure_keeps_inline_and_does_not_raise(s3):
    s3.fail_puts = 99
    row = _row()
    db = FakeDB(row)
    assert _run(ap.archive(db, 1, ap.REQUEST)) is False
    assert s3.put_calls == ap.UPLOAD_ATTEMPTS
    assert row.request_object_key is None and row.messages is not None
    assert ap.status(row) == "pending"
    _run(ap.archive_quietly(db, 1, ap.REQUEST))  # swallowed, no exception


def test_unverified_upload_does_not_clear_inline(s3, monkeypatch):
    monkeypatch.setattr(s3, "head_object", lambda **kw: {"ContentLength": 0})
    row = _row()
    assert _run(ap.archive(FakeDB(row), 1, ap.REQUEST)) is False
    assert row.messages is not None and row.request_object_key is None


def test_sweeper_retries_after_failure(s3):
    s3.fail_puts = 99
    row = _row()
    db = FakeDB(row)
    _run(ap.archive(db, 1, ap.REQUEST))
    s3.fail_puts = 0
    assert _run(ap.sweep_once(db)) == 1
    assert row.request_object_key and row.messages is None


# --- backfill -----------------------------------------------------------------


def test_backfill_interrupted_then_resumed_is_idempotent(s3):
    row = _row(response={"r": 1}, completed_at=datetime.now(UTC))
    db = FakeDB(row)
    # interrupted after the request half only
    _run(ap.archive(db, 1, ap.REQUEST))
    assert row.response == {"r": 1}
    _run(ap.sweep_once(db))
    assert row.response is None and row.response_object_key
    puts = s3.put_calls
    _run(ap.sweep_once(db))
    assert s3.put_calls == puts  # nothing re-uploaded


def test_sweep_query_compiles_and_skips_archived():
    stmt_sql = None

    class Capture(FakeDB):
        async def exec(self, stmt):
            nonlocal stmt_sql
            stmt_sql = str(stmt.compile())
            return SimpleNamespace(all=lambda: [])

    with patch.dict(os.environ, {"PIONEER_API_LOG_BUCKET": "b"}):
        assert _run(ap.sweep_once(Capture())) == 0
    assert "request_object_key IS NULL" in stmt_sql and "response_object_key IS NULL" in stmt_sql


# --- reads --------------------------------------------------------------------


def test_read_archived_payload(s3):
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))
    out = _run(ap.read_payload(row))
    assert out["system"] == [{"type": "text", "text": "sys"}] and out["extra"] == {"max_tokens": 10}


def test_legacy_inline_row_reads_without_s3(s3):
    row = _row(response={"r": 1})
    out = _run(ap.read_payload(row))
    assert out["messages"] and out["response"] == {"r": 1} and s3.get_calls == 0


def test_expired_payload_distinct_from_missing(s3):
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))
    del s3.objects[row.request_object_key]
    with pytest.raises(ap.PayloadUnavailable) as missing:
        _run(ap.read_payload(row))
    assert missing.value.reason == "missing"
    row.payload_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    gets = s3.get_calls
    with pytest.raises(ap.PayloadUnavailable) as expired:
        _run(ap.read_payload(row))
    assert expired.value.reason == "expired" and s3.get_calls == gets  # no S3 call
    assert ap.status(row) == "expired"


# --- routes: authorization + no payload reads for metadata --------------------


def _routes():
    import routes.foreman as r

    return r


def _call_get(row, user, role, include_payload):
    r = _routes()

    class DB(FakeDB):
        async def exec(self, stmt):
            self.selects += 1
            if self.selects == 1:
                return SimpleNamespace(one_or_none=lambda: 7)
            return SimpleNamespace(one_or_none=lambda: row)

    with patch.object(r, "ensure_membership", AsyncMock(return_value=role)):
        return _run(r.get_api_request("g", 1, include_payload, user, DB()))


def test_metadata_query_never_reads_s3(s3):
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))
    out = _call_get(row, "u1", "member", include_payload=False)
    assert "payload" not in out and out["payload_status"] == "archived"
    assert out["input_tokens"] == 1 and s3.get_calls == 0


def test_list_never_reads_s3(s3):
    r = _routes()
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))

    class DB(FakeDB):
        n = 0

        async def exec(self, stmt):
            self.n += 1
            if self.n == 1:
                return SimpleNamespace(one_or_none=lambda: 7)
            return SimpleNamespace(all=lambda: [row])

    with patch.object(r, "ensure_membership", AsyncMock(return_value="owner")):
        out = _run(r.list_api_requests("g", 50, "boss", DB()))
    assert out["requests"][0]["payload_status"] == "archived" and s3.get_calls == 0
    assert "payload" not in out["requests"][0]


def test_authorized_user_reads_payload_lazily(s3):
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))
    out = _call_get(row, "u1", "member", include_payload=True)
    assert out["payload"]["messages"][0]["content"] == "hi" and s3.get_calls == 1


def test_guild_owner_can_read_other_users_payload(s3):
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))
    assert "payload" in _call_get(row, "boss", "owner", include_payload=True)


def test_other_member_is_denied(s3):
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))
    with pytest.raises(HTTPException) as e:
        _call_get(row, "intruder", "member", include_payload=True)
    assert e.value.status_code == 404 and s3.get_calls == 0


def test_expired_payload_returns_410_and_missing_404(s3):
    row = _row()
    _run(ap.archive(FakeDB(row), 1, ap.REQUEST))
    row.payload_expires_at = datetime.now(UTC) - timedelta(days=1)
    with pytest.raises(HTTPException) as e:
        _call_get(row, "u1", "member", include_payload=True)
    assert e.value.status_code == 410 and e.value.detail == {"payload_status": "expired"}
    row.payload_expires_at = datetime.now(UTC) + timedelta(days=1)
    del s3.objects[row.request_object_key]
    with pytest.raises(HTTPException) as e:
        _call_get(row, "u1", "member", include_payload=True)
    assert e.value.status_code == 404 and e.value.detail == {"payload_status": "missing"}


# --- runner integration: logging independent of payload availability ---------


def test_runner_create_and_complete_survive_archive_failure(monkeypatch):
    from foreman import runner

    class DB(FakeDB):
        def add(self, obj):
            obj.id = 1
            self.added = obj

        async def refresh(self, obj):
            pass

    db = DB()
    monkeypatch.setattr(runner, "get_db", AsyncMock(return_value=db))
    monkeypatch.setattr(
        runner.audit_payloads, "archive", AsyncMock(side_effect=RuntimeError("boom"))
    )
    log_id = _run(runner._create_api_request_log("m", [], [], {"a": 1}))
    assert log_id == 1 and db.added.messages == []
    db.exec = AsyncMock()
    _run(
        runner._complete_api_request_log(
            1,
            request_id="r",
            response_dict={},
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            stop_reason="end_turn",
        )
    )
    assert db.exec.await_count == 1


def test_model_has_reference_columns():
    cols = ApiRequestLog.__table__.c
    for name in (
        "request_object_key",
        "response_object_key",
        "payload_archived_at",
        "payload_expires_at",
        "task_id",
        "guild_id",
        "user_id",
        "input_tokens",
    ):
        assert name in cols
