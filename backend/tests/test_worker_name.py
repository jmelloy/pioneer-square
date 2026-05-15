"""Tests for worker identity: name = hostname[:3]/worker_id."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import worker_display_name

sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, make_auth_token

# ---------------------------------------------------------------------------
# worker_display_name unit tests
# ---------------------------------------------------------------------------


def test_with_hostname_normal():
    assert worker_display_name("w-g2otus", "token-machine") == "tok/w-g2otus"


def test_with_hostname_takes_exactly_3_chars():
    assert worker_display_name("w-abc123", "xy") == "xy/w-abc123"


def test_with_hostname_single_char():
    assert worker_display_name("w-abc123", "z") == "z/w-abc123"


def test_with_hostname_exactly_3_chars():
    assert worker_display_name("w-abc123", "tok") == "tok/w-abc123"


def test_with_hostname_longer_than_3():
    assert worker_display_name("w-abc123", "foobar") == "foo/w-abc123"


def test_without_hostname_returns_worker_id():
    assert worker_display_name("w-abc123") == "w-abc123"


def test_without_hostname_explicit_none():
    assert worker_display_name("w-abc123", None) == "w-abc123"


def test_hostname_empty_string():
    # Empty hostname — falsy, so treated as absent; returns bare worker_id.
    assert worker_display_name("w-abc123", "") == "w-abc123"


def test_name_contains_worker_id():
    wid = "w-g2otus"
    name = worker_display_name(wid, "pioneer")
    assert wid in name


def test_name_contains_hostname_prefix():
    name = worker_display_name("w-g2otus", "pioneer")
    assert name.startswith("pio/")


# ---------------------------------------------------------------------------
# API tests: registration returns name; list includes name
# ---------------------------------------------------------------------------


def _auth(db_path: str) -> dict:
    return {"Authorization": f"Bearer {make_auth_token(db_path)}"}


def test_register_worker_name_without_hostname(client):
    """When no hostname is supplied the name falls back to the worker_id."""
    test_client, db_path = client
    insert_guild(db_path, "wname01")
    resp = test_client.post("/guilds/wname01/workers", json={"repos": []})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == data["id"]


def test_register_worker_name_with_hostname(client):
    """Registration with a hostname produces ``hostname[:3]/worker_id``."""
    test_client, db_path = client
    insert_guild(db_path, "wname02")
    resp = test_client.post(
        "/guilds/wname02/workers",
        json={"repos": [], "hostname": "pioneer-box"},
    )
    assert resp.status_code == 200
    data = resp.json()
    wid = data["id"]
    assert data["name"] == f"pio/{wid}"


def test_register_worker_name_short_hostname(client):
    """Hostnames shorter than 3 chars are used in full."""
    test_client, db_path = client
    insert_guild(db_path, "wname03")
    resp = test_client.post(
        "/guilds/wname03/workers",
        json={"repos": [], "hostname": "ab"},
    )
    assert resp.status_code == 200
    data = resp.json()
    wid = data["id"]
    assert data["name"] == f"ab/{wid}"


def test_list_workers_includes_name(client):
    """GET /workers response includes a ``name`` field for each worker."""
    test_client, db_path = client
    insert_guild(db_path, "wname04")
    create_resp = test_client.post(
        "/guilds/wname04/workers",
        json={"repos": [], "hostname": "testhost"},
    )
    assert create_resp.status_code == 200
    wid = create_resp.json()["id"]

    list_resp = test_client.get("/guilds/wname04/workers", headers=_auth(db_path))
    assert list_resp.status_code == 200
    workers = list_resp.json()
    assert len(workers) == 1
    assert "name" in workers[0]
    assert workers[0]["name"] == f"tes/{wid}"


def test_list_workers_name_persisted_correctly(client):
    """The name stored in the DB matches what was returned at registration."""
    import sqlite3

    test_client, db_path = client
    insert_guild(db_path, "wname05")
    resp = test_client.post(
        "/guilds/wname05/workers",
        json={"repos": [], "hostname": "myhost"},
    )
    wid = resp.json()["id"]
    expected_name = f"myh/{wid}"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT name FROM workers WHERE id = ?", (wid,)).fetchone()
    assert row is not None
    assert row[0] == expected_name
