"""Tests for worker identity: droid-style name derived from worker ID."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import format_worker_id, worker_display_name

sys.path.insert(0, os.path.dirname(__file__))
from helpers import _sync_session, insert_guild
from models import Worker
from sqlalchemy import select
from sqlmodel import col  # noqa: E402

# ---------------------------------------------------------------------------
# format_worker_id unit tests
# ---------------------------------------------------------------------------


def test_format_standard():
    assert format_worker_id("w-vd3566") == "VD-3566"


def test_format_ab1234():
    assert format_worker_id("w-ab1234") == "AB-1234"


def test_format_single_letter():
    assert format_worker_id("w-x9") == "X-9"


def test_format_three_letters():
    assert format_worker_id("w-abc123") == "ABC-123"


def test_format_letter_digit_letters():
    # Letters end at 'g', digits+letters follow from '2otus'
    assert format_worker_id("w-g2otus") == "G-2OTUS"


def test_format_strips_w_prefix():
    name = format_worker_id("w-vd3566")
    assert not name.startswith("w-")
    assert not name.startswith("W-")


def test_format_uppercase():
    assert format_worker_id("w-vd3566") == format_worker_id("w-vd3566").upper()


# Edge cases — mirrored in frontend/src/utils/__tests__/format.spec.ts


def test_format_all_digit_suffix():
    # m.start() == 0 → no hyphen inserted, digits returned as-is
    assert format_worker_id("w-1234") == "1234"


def test_format_no_digit_suffix():
    # No digit match → whole string uppercased, no hyphen
    assert format_worker_id("w-abc") == "ABC"


# ---------------------------------------------------------------------------
# worker_display_name: delegates to format_worker_id, ignores hostname
# ---------------------------------------------------------------------------


def test_worker_display_name_with_hostname():
    assert worker_display_name("w-vd3566", "token-machine") == "token-machine/VD-3566"


def test_worker_display_name_no_hostname():
    assert worker_display_name("w-abc123") == "ABC-123"


def test_worker_display_name_none_hostname():
    assert worker_display_name("w-abc123", None) == "ABC-123"


def test_worker_display_name_empty_hostname():
    assert worker_display_name("w-abc123", "") == "ABC-123"


def test_worker_display_name_with_hostname_format():
    wid = "w-vd3566"
    assert worker_display_name(wid, "some-host") == f"some-host/{format_worker_id(wid)}"


# ---------------------------------------------------------------------------
# API tests: registration returns droid-style name
# ---------------------------------------------------------------------------


def test_register_worker_name_without_hostname(client):
    """Name is the droid-formatted worker ID even without a hostname."""
    test_client, db_url = client
    insert_guild(db_url, "wname01")
    resp = test_client.post("/guilds/wname01/workers", json={"repos": []})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == format_worker_id(data["id"])


def test_register_worker_name_with_hostname(client):
    """Name is ``hostname/droid-ID`` when a hostname is provided."""
    test_client, db_url = client
    insert_guild(db_url, "wname02")
    resp = test_client.post(
        "/guilds/wname02/workers",
        json={"repos": [], "hostname": "pioneer-box"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == f"pioneer-box/{format_worker_id(data['id'])}"


def test_register_worker_name_short_hostname(client):
    """Short hostnames are still prepended."""
    test_client, db_url = client
    insert_guild(db_url, "wname03")
    resp = test_client.post(
        "/guilds/wname03/workers",
        json={"repos": [], "hostname": "ab"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == f"ab/{format_worker_id(data['id'])}"


def test_list_workers_includes_name(client):
    """The worker row persists a ``name`` derived from hostname + worker id."""
    test_client, db_url = client
    insert_guild(db_url, "wname04")
    create_resp = test_client.post(
        "/guilds/wname04/workers",
        json={"repos": [], "hostname": "testhost"},
    )
    assert create_resp.status_code == 200
    wid = create_resp.json()["id"]

    with _sync_session(db_url) as session:
        name = session.scalar(select(col(Worker.name)).where(col(Worker.id) == wid))
    assert name == f"testhost/{format_worker_id(wid)}"


def test_list_workers_name_persisted_correctly(client):
    """The name stored in the DB matches what was returned at registration."""
    test_client, db_url = client
    insert_guild(db_url, "wname05")
    resp = test_client.post(
        "/guilds/wname05/workers",
        json={"repos": [], "hostname": "myhost"},
    )
    wid = resp.json()["id"]
    expected_name = f"myhost/{format_worker_id(wid)}"

    with _sync_session(db_url) as session:
        name = session.scalar(select(col(Worker.name)).where(col(Worker.id) == wid))
    assert name is not None
    assert name == expected_name
