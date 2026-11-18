"""Tests for the S3 archive of Foreman API audit payloads (api_audit_store, runner hooks)."""

from __future__ import annotations

import gzip
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api_audit_store as store
from foreman import runner
from models import ApiRequestLog


class ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, fail_puts: int = 0):
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict] = []
        self.get_calls: list[str] = []
        self.fail_puts = fail_puts

    def put_object(self, **kw):
        self.put_calls.append(kw)
        if self.fail_puts > 0:
            self.fail_puts -= 1
            raise ClientError("InternalError")
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, *, Bucket, Key):
        self.get_calls.append(Key)
        if Key not in self.objects:
            raise ClientError("NoSuchKey")
        body = MagicMock()
        body.read.return_value = self.objects[Key]
        return {"Body": body}


@pytest.fixture()
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("PIONEER_AUDIT_BUCKET", "audit-bucket")
    monkeypatch.setattr(store, "_client", fake)
    monkeypatch.setattr(store, "RETRY_BASE_DELAY", 0)
    return fake


@pytest.fixture()
def no_s3(monkeypatch):
    monkeypatch.delenv("PIONEER_AUDIT_BUCKET", raising=False)


def _session(first_value=None):
    session = AsyncMock()
    session.add = MagicMock()
    added: list = []
    session.add.side_effect = added.append

    async def _refresh(obj):
        obj.id = 7

    session.refresh = AsyncMock(side_effect=_refresh)
    session.exec = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=first_value)))
    session.added = added
    return session


SYSTEM = [{"type": "text", "text": "You are the foreman. " * 50}]
MESSAGES = [{"role": "user", "content": "hello"}]
EXTRA = {"max_tokens": 10, "tools": []}


async def _create(session):
    with (
        patch.object(runner, "get_db", AsyncMock(return_value=session)),
        patch.object(runner, "get_guild_pk", AsyncMock(return_value=3)),
    ):
        return await runner._create_api_request_log(
            "m", SYSTEM, MESSAGES, EXTRA, task_id=None, guild_id="g1", user_id="u", trigger="chat"
        )


# --- archival -------------------------------------------------------------------------


async def test_request_archived_to_s3_not_inline(s3):
    session = _session()
    assert await _create(session) == 7
    row = session.added[0]
    assert row.system is None and row.messages is None and row.extra is None
    assert row.request_object_key.startswith("foreman-api-audit/3/")
    assert row.request_object_key.endswith("/request.json.gz")
    body = s3.objects[row.request_object_key]
    assert json.loads(gzip.decompress(body)) == {
        "system": SYSTEM,
        "messages": MESSAGES,
        "extra": EXTRA,
    }
    assert row.request_object_bytes == len(body)
    assert len(row.request_sha256) == 64
    assert s3.put_calls[0]["ServerSideEncryption"] == "AES256"
    # metadata stays queryable
    assert (row.guild_id, row.user_id, row.trigger, row.model) == (3, "u", "chat", "m")


async def test_response_archived_and_metadata_updated(s3):
    session = _session(first_value=3)
    with patch.object(runner, "get_db", AsyncMock(return_value=session)):
        await runner._complete_api_request_log(
            7,
            request_id="req_1",
            response_dict={"content": [{"type": "text", "text": "hi"}]},
            input_tokens=5,
            output_tokens=6,
            cache_read_tokens=1,
            cache_write_tokens=2,
            stop_reason="end_turn",
        )
    stmt = session.exec.await_args_list[-1].args[0]
    params = stmt.compile().params
    assert params["response_object_key"].endswith("/response.json.gz")
    assert params["input_tokens"] == 5 and params["stop_reason"] == "end_turn"
    assert "response" not in params
    assert json.loads(gzip.decompress(s3.objects[params["response_object_key"]]))["content"]


async def test_archival_disabled_stores_inline(no_s3):
    session = _session()
    await _create(session)
    row = session.added[0]
    assert row.system == SYSTEM and row.messages == MESSAGES and row.extra == EXTRA
    assert row.request_object_key is None


# --- failure / retry ------------------------------------------------------------------


async def test_upload_retries_then_succeeds(s3):
    s3.fail_puts = 2
    session = _session()
    await _create(session)
    assert len(s3.put_calls) == 3
    assert session.added[0].request_object_key in s3.objects


async def test_upload_failure_falls_back_inline_without_raising(s3):
    s3.fail_puts = 99
    session = _session()
    await _create(session)
    assert len(s3.put_calls) == 3  # default attempts
    row = session.added[0]
    assert row.request_object_key is None
    assert row.messages == MESSAGES and row.system == SYSTEM
    assert not s3.objects


async def test_response_upload_failure_falls_back_inline(s3):
    s3.fail_puts = 99
    cols = await store.archive_response(guild_pk=1, response={"a": 1})
    assert cols == {"response": {"a": 1}}


# --- lazy reads & authorization -------------------------------------------------------


async def _archived_row(s3, guild_id=3):
    session = _session()
    await _create(session)
    row = session.added[0]
    resp = await store.archive_response(guild_pk=3, response={"ok": True})
    for k, v in resp.items():
        setattr(row, k, v)
    row.guild_id = guild_id
    return row


async def test_authorized_lazy_read(s3):
    row = await _archived_row(s3)
    s3.get_calls.clear()
    out = await store.read_payloads(row, guild_pk=3)
    assert out["system"] == SYSTEM and out["messages"] == MESSAGES and out["extra"] == EXTRA
    assert out["response"] == {"ok": True} and out["expired"] == []
    assert len(s3.get_calls) == 2


async def test_request_only_read_skips_response_object(s3):
    row = await _archived_row(s3)
    s3.get_calls.clear()
    await store.read_payloads(row, guild_pk=3, include_response=False)
    assert s3.get_calls == [row.request_object_key]


@pytest.mark.parametrize("caller", [4, None])
async def test_unauthorized_read_denied_before_s3(s3, caller):
    row = await _archived_row(s3)
    s3.get_calls.clear()
    with pytest.raises(store.AuditAccessDenied):
        await store.read_payloads(row, guild_pk=caller)
    assert s3.get_calls == []


async def test_metadata_query_never_reads_payloads(s3):
    session = AsyncMock()
    rows = MagicMock()
    rows.all.return_value = [tuple(range(len(store._METADATA_FIELDS)))]
    session.exec = AsyncMock(return_value=rows)
    out = await store.list_metadata(session, guild_pk=3)
    assert out[0]["id"] == 0
    sql = str(session.exec.await_args.args[0].compile())
    selected = sql.split("FROM")[0]
    for payload_col in ("system", "messages", "response,", "extra", "request_object_key"):
        assert payload_col not in selected
    assert s3.get_calls == [] and s3.put_calls == []


# --- legacy fallback / expiry ---------------------------------------------------------


async def test_legacy_inline_row_readable_without_s3(no_s3):
    row = SimpleNamespace(
        guild_id=3,
        system=SYSTEM,
        messages=MESSAGES,
        extra=EXTRA,
        response={"legacy": 1},
        request_object_key=None,
        response_object_key=None,
    )
    out = await store.read_payloads(row, guild_pk=3)
    assert out["messages"] == MESSAGES and out["response"] == {"legacy": 1}


async def test_expired_object_reported_not_raised(s3):
    row = await _archived_row(s3)
    del s3.objects[row.response_object_key]
    out = await store.read_payloads(row, guild_pk=3)
    assert out["expired"] == ["response"]
    assert out["response"] is None
    assert out["messages"] == MESSAGES


async def test_other_read_errors_propagate(s3, monkeypatch):
    row = await _archived_row(s3)
    monkeypatch.setattr(s3, "get_object", MagicMock(side_effect=ClientError("AccessDenied")))
    with pytest.raises(store.AuditStoreError):
        await store.read_payloads(row, guild_pk=3)


# --- backfill -------------------------------------------------------------------------


class BackfillDB:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    async def exec(self, stmt):
        if stmt.is_select:
            pending = [r for r in self.rows if r.request_object_key is None and r.system]
            return MagicMock(all=MagicMock(return_value=pending))
        values = {c.key: v.value for c, v in stmt._values.items()}
        target = next(r for r in self.rows if r.id == stmt.compile().params["id_1"])
        for k, v in values.items():
            setattr(target, k, v)
        self.updates.append(values)
        return MagicMock(rowcount=1)

    async def commit(self):
        pass


async def test_backfill_is_idempotent(s3):
    from datetime import UTC, datetime

    rows = [
        SimpleNamespace(
            id=i,
            guild_id=3,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            system=SYSTEM,
            messages=MESSAGES,
            extra=EXTRA,
            response={"r": i},
            request_object_key=None,
            response_object_key=None,
        )
        for i in (1, 2)
    ]
    db = BackfillDB(rows)
    assert await store.backfill_legacy_rows(db) == 2
    assert all(r.system is None and r.response is None for r in rows)
    assert all(r.request_object_key in s3.objects for r in rows)
    puts = len(s3.put_calls)
    assert await store.backfill_legacy_rows(db) == 0
    assert len(s3.put_calls) == puts
    out = await store.read_payloads(rows[0], guild_pk=3)
    assert out["system"] == SYSTEM and out["response"] == {"r": 1}


async def test_backfill_upload_failure_keeps_inline_data(s3):
    from datetime import UTC, datetime

    row = SimpleNamespace(
        id=1,
        guild_id=3,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        system=SYSTEM,
        messages=MESSAGES,
        extra=None,
        response=None,
        request_object_key=None,
        response_object_key=None,
    )
    s3.fail_puts = 99
    db = BackfillDB([row])
    assert await store.backfill_legacy_rows(db) == 0
    assert row.system == SYSTEM and db.updates == []


def test_model_has_reference_columns():
    for c in ("request_object_key", "response_object_key", "request_sha256", "response_sha256"):
        assert c in ApiRequestLog.model_fields
