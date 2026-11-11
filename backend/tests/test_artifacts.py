"""Tests for artifact uploads (backend/artifact_service.py, routes/artifacts.py)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import artifact_service
from helpers import insert_guild, insert_member, make_auth_token

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_validate_accepts_png_and_alias():
    assert artifact_service.validate(PNG, "image/png", 1000) == "image/png"
    jpg = b"\xff\xd8\xff" + b"0" * 10
    assert artifact_service.validate(jpg, "image/jpg", 1000) == "image/jpeg"


@pytest.mark.parametrize(
    "data,ctype,status",
    [
        (b"", "image/png", 400),
        (PNG, "image/svg+xml", 415),
        (b"not an image", "image/png", 415),
        (PNG + b"0" * 2000, "image/png", 413),
    ],
)
def test_validate_rejects(data, ctype, status):
    with pytest.raises(artifact_service.ArtifactError) as ei:
        artifact_service.validate(data, ctype, 1000)
    assert ei.value.status_code == status


def test_upload_route(client, monkeypatch):
    c, db = client
    insert_guild(db, "art1")
    insert_member(db, "art1", "m-art1", "member")
    tok = make_auth_token(db, user_id="m-art1", username="m-art1")
    h = {"Authorization": f"Bearer {tok}"}

    async def fake_store(data, ctype, guild, task_id):
        c_ = artifact_service.validate(data, ctype, 1000)
        return artifact_service.StoredObject(
            "id-1", f"a/{guild}/id-1.png", "https://x/y.png", c_, len(data)
        )

    monkeypatch.setattr(artifact_service, "store", fake_store)
    r = c.post("/guilds/art1/artifacts", files={"file": ("s.png", PNG, "image/png")}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://x/y.png"
    bad = c.post("/guilds/art1/artifacts", files={"file": ("s.png", b"zz", "image/png")}, headers=h)
    assert bad.status_code == 415
    assert (
        c.post("/guilds/art1/artifacts", files={"file": ("s.png", PNG, "image/png")}).status_code
        == 401
    )


def test_markdown_reference_uses_artifact_scheme():
    from routes.artifacts import _link_text

    assert _link_text("a/b/shot [1].png") == "shot 1.png"
    assert _link_text(None) == "artifact"
