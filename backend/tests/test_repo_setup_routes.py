"""Tests for per-repository setup command routes (backend/routes/repo_setup.py)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from helpers import (
    _sync_session,
    insert_guild,
    insert_member,
    insert_worker,
    make_auth_token,
)
from models import Guild, RepositorySetupSettings
from sqlalchemy import select
from sqlmodel import col


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _setup_guild(db_url: str, slug: str):
    insert_guild(db_url, slug)
    member_token = make_auth_token(db_url, user_id=f"m-{slug}", username=f"m-{slug}")
    insert_member(db_url, slug, f"m-{slug}", "member")
    viewer_token = make_auth_token(db_url, user_id=f"v-{slug}", username=f"v-{slug}")
    insert_member(db_url, slug, f"v-{slug}", "viewer")
    outsider = make_auth_token(db_url, user_id=f"o-{slug}", username=f"o-{slug}")
    return member_token, viewer_token, outsider


def test_roundtrip_persists_and_normalizes(client):
    c, db = client
    member, _, _ = _setup_guild(db, "rsetup1")
    url = "/guilds/rsetup1/repos/Acme/Widget/setup"

    assert c.get(url, headers=_h(member)).json()["setupCommands"] == []

    r = c.put(
        url,
        json={"setup_commands": ["  npm ci ", "", "npm run build"], "timeout_seconds": 120},
        headers=_h(member),
    )
    assert r.status_code == 200, r.text
    got = c.get("/guilds/rsetup1/repos/acme/widget/setup", headers=_h(member)).json()
    assert got["setupCommands"] == ["npm ci", "npm run build"]
    assert got["timeoutSeconds"] == 120

    # Second PUT updates in place (single row).
    c.put(url, json={"setup_commands": ["make"], "timeout_seconds": 30}, headers=_h(member))
    with _sync_session(db) as s:
        pk = s.scalar(select(col(Guild.id)).where(col(Guild.slug) == "rsetup1"))
        rows = s.scalars(
            select(RepositorySetupSettings).where(col(RepositorySetupSettings.guild_id) == pk)
        ).all()
    assert len(rows) == 1 and rows[0].setup_commands == ["make"]

    assert c.delete(url, headers=_h(member)).status_code == 200
    assert c.get(url, headers=_h(member)).json()["setupCommands"] == []


def test_authorization(client):
    c, db = client
    member, viewer, outsider = _setup_guild(db, "rsetup2")
    url = "/guilds/rsetup2/repos/acme/widget/setup"
    body = {"setup_commands": ["make"], "timeout_seconds": 60}

    assert c.put(url, json=body).status_code == 401
    assert c.put(url, json=body, headers=_h(viewer)).status_code == 403
    assert c.put(url, json=body, headers=_h(outsider)).status_code == 403
    assert c.delete(url, headers=_h(viewer)).status_code == 403
    assert c.get(url, headers=_h(outsider)).status_code == 403
    assert c.get(url).status_code == 401
    # Viewers may read.
    assert c.get(url, headers=_h(viewer)).status_code == 200


def test_worker_can_read_but_not_write(client):
    c, db = client
    member, _, _ = _setup_guild(db, "rsetup3")
    insert_worker(db, "rsetup3", "w-rsetup3")
    url = "/guilds/rsetup3/repos/acme/widget/setup"
    c.put(url, json={"setup_commands": ["make"], "timeout_seconds": 60}, headers=_h(member))
    with _sync_session(db) as s:
        from models import Worker

        tok = s.scalar(select(col(Worker.auth_token)).where(col(Worker.id) == "w-rsetup3"))
    if tok:
        assert c.get(url, headers=_h(tok)).json()["setupCommands"] == ["make"]
        assert c.put(url, json={"setup_commands": []}, headers=_h(tok)).status_code in (401, 403)


def test_validation_and_scoping(client):
    c, db = client
    member, _, _ = _setup_guild(db, "rsetup4")
    _setup_guild(db, "rsetup5")
    url = "/guilds/rsetup4/repos/acme/widget/setup"
    assert (
        c.put(url, json={"setup_commands": ["x"], "timeout_seconds": 0}, headers=_h(member))
    ).status_code == 422
    assert (c.put(url, json={"setup_commands": ["x"] * 51}, headers=_h(member))).status_code == 422
    c.put(url, json={"setup_commands": ["make"]}, headers=_h(member))
    # Another guild's same repo is unaffected.
    other = make_auth_token(db, user_id="m-rsetup5", username="m-rsetup5")
    assert (
        c.get("/guilds/rsetup5/repos/acme/widget/setup", headers=_h(other)).json()["setupCommands"]
        == []
    )
