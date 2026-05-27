"""Unit tests for ws_dumps in events.py."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime, timezone
from uuid import UUID

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from events import ws_dumps  # noqa: E402


def test_datetime_serializes_to_iso8601():
    dt = datetime(2024, 3, 15, 10, 30, 0, tzinfo=UTC)
    result = json.loads(ws_dumps({"ts": dt}))
    assert result["ts"] == "2024-03-15T10:30:00+00:00"


def test_date_serializes_to_iso8601():
    d = date(2024, 3, 15)
    result = json.loads(ws_dumps({"d": d}))
    assert result["d"] == "2024-03-15"


def test_uuid_serializes_to_string():
    uid = UUID("12345678-1234-5678-1234-567812345678")
    result = json.loads(ws_dumps({"id": uid}))
    assert result["id"] == "12345678-1234-5678-1234-567812345678"


def test_plain_dict_passes_through():
    payload = {"type": "ping", "value": 42, "flag": True, "none": None}
    result = json.loads(ws_dumps(payload))
    assert result == payload


def test_unhandled_type_raises_type_error():
    class _Custom:
        pass

    with pytest.raises(TypeError):
        ws_dumps({"obj": _Custom()})
