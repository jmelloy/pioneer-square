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
        (b"\xff\xfe bad utf8", "text/html", 415),
        (b"a\x00b", "text/plain", 415),
        (b"<html>", "application/pdf", 415),
    ],
)
def test_validate_rejects(data, ctype, status):
    with pytest.raises(artifact_service.ArtifactError) as ei:
        artifact_service.validate(data, ctype, 1000)
    assert ei.value.status_code == status


def test_validate_accepts_documents():
    assert artifact_service.validate(b"<h1>hi</h1>", "text/html; charset=utf-8", 1000) == "text/html"
    assert artifact_service.validate(b"%PDF-1.7", "application/pdf", 1000) == "application/pdf"
    assert artifact_service.artifact_type("text/markdown") == "text"
    assert artifact_service.artifact_type("image/gif") == "image"


def test_html_upload_is_typed_and_sandboxed(client, monkeypatch):
    c, db = client
    insert_guild(db, "art2")
    insert_member(db, "art2", "m-art2", "member")
    h = {"Authorization": f"Bearer {make_auth_token(db, user_id='m-art2', username='m-art2')}"}
    html = b"<script>alert(1)</script>"

    async def fake_store(data, ctype, guild, task_id):
        c_ = artifact_service.validate(data, ctype, 1000)
        t = artifact_service.artifact_type(c_)
        return artifact_service.StoredObject("id-h", "a/id-h.html", "u", c_, len(data), t)

    monkeypatch.setattr(artifact_service, "store", fake_store)
    files = {"file": ("r.html", html, "text/html")}
    r = c.post("/guilds/art2/artifacts", files=files, data={"type": "html"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "html"
    assert body["markdown"] == "[r.html](artifact://id-h?type=html)"
    wrong = c.post("/guilds/art2/artifacts", files=files, data={"type": "image"}, headers=h)
    assert wrong.status_code == 415

    class Body:
        def iter_chunks(self, size):
            yield html

    monkeypatch.setattr(artifact_service, "open_object", lambda key: Body())
    pub = c.get("/api/artifacts/id-h/public")
    assert pub.headers["content-type"].startswith("text/html")
    assert pub.headers["content-security-policy"] == "sandbox allow-scripts"


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
    # Payload always carries the permanent proxy URL, never the S3 one.
    assert r.json()["url"] == "/api/artifacts/id-1/public"

    class Body:
        def iter_chunks(self, size):
            yield PNG

    seen = []
    monkeypatch.setattr(artifact_service, "open_object", lambda key: seen.append(key) or Body())
    pub = c.get("/api/artifacts/id-1/public")  # no auth: the UUID is the capability
    assert pub.status_code == 200, pub.text
    assert pub.content == PNG and seen == ["a/art1/id-1.png"]
    assert pub.headers["content-type"] == "image/png"
    assert "immutable" in pub.headers["cache-control"]
    assert "content-security-policy" not in pub.headers
    assert r.json()["type"] == "image" and r.json()["markdown"] == "[s.png](artifact://id-1)"
    assert c.get("/api/artifacts/nope/public").status_code == 404
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


def test_presigned_url_is_regional_sigv4(monkeypatch):
    """Global-endpoint SigV2 URLs get redirected by S3 for non-us-east-1 buckets (#1464)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    monkeypatch.setattr(artifact_service, "_client", None)
    cfg = artifact_service.ArtifactConfig("my-bucket", "artifacts", "us-west-2", None, 1000)
    url = artifact_service.build_url(cfg, "artifacts/g/t/a.png")
    monkeypatch.setattr(artifact_service, "_client", None)
    assert url.startswith("https://my-bucket.s3.us-west-2.amazonaws.com/artifacts/g/t/a.png?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
