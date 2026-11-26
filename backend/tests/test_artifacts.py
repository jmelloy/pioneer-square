"""Tests for artifact uploads (backend/artifact_service.py, routes/artifacts.py)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import artifact_service
from helpers import (
    _sync_session,
    insert_conversation,
    insert_guild,
    insert_member,
    insert_task,
    insert_worker,
    make_auth_token,
)
from models import Worker

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
    assert (
        artifact_service.validate(b"<h1>hi</h1>", "text/html; charset=utf-8", 1000) == "text/html"
    )
    assert artifact_service.validate(b"%PDF-1.7", "application/pdf", 1000) == "application/pdf"
    assert artifact_service.artifact_type("text/markdown") == "text"
    assert artifact_service.artifact_type("image/gif") == "image"


def test_html_upload_is_typed_and_sandboxed(client, monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://pioneer.example/")
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
    assert (
        body["markdown"] == "[r.html](https://pioneer.example/api/artifacts/id-h/public?type=html)"
    )
    wrong = c.post("/guilds/art2/artifacts", files=files, data={"type": "image"}, headers=h)
    assert wrong.status_code == 415

    class Body:
        def iter_chunks(self, size):
            yield html

    monkeypatch.setattr(artifact_service, "open_object", lambda key: Body())
    pub = c.get("/api/artifacts/id-h/public")
    assert pub.headers["content-type"].startswith("text/html")
    assert pub.headers["content-security-policy"] == "sandbox allow-scripts"


@pytest.mark.parametrize(
    "public_origin,artifact_id", [(None, "id-1"), ("https://pioneer.example/", "id-2")]
)
def test_upload_route(client, monkeypatch, public_origin, artifact_id):
    if public_origin:
        monkeypatch.setenv("FRONTEND_URL", public_origin)
    else:
        monkeypatch.delenv("FRONTEND_URL", raising=False)
    c, db = client
    insert_guild(db, "art1")
    insert_member(db, "art1", "m-art1", "member")
    tok = make_auth_token(db, user_id="m-art1", username="m-art1")
    h = {"Authorization": f"Bearer {tok}"}

    async def fake_store(data, ctype, guild, task_id):
        c_ = artifact_service.validate(data, ctype, 1000)
        return artifact_service.StoredObject(
            artifact_id, f"a/{guild}/{artifact_id}.png", "https://x/y.png", c_, len(data)
        )

    monkeypatch.setattr(artifact_service, "store", fake_store)
    r = c.post("/guilds/art1/artifacts", files={"file": ("s.png", PNG, "image/png")}, headers=h)
    assert r.status_code == 200, r.text
    # Payload always carries the permanent proxy URL, never the S3 one.
    assert r.json()["url"] == f"/api/artifacts/{artifact_id}/public"
    base = (public_origin or str(c.base_url)).rstrip("/")
    public_url = base + f"/api/artifacts/{artifact_id}/public"
    assert r.json()["public_url"] == public_url
    metadata = c.get(f"/guilds/art1/artifacts/{artifact_id}", headers=h)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["public_url"] == public_url

    class Body:
        def iter_chunks(self, size):
            yield PNG

    seen = []
    monkeypatch.setattr(artifact_service, "open_object", lambda key: seen.append(key) or Body())
    pub = c.get(public_url)  # no auth: the UUID is the capability
    assert pub.status_code == 200, pub.text
    assert pub.content == PNG and seen == [f"a/art1/{artifact_id}.png"]
    assert pub.headers["content-type"] == "image/png"
    assert "immutable" in pub.headers["cache-control"]
    assert "content-security-policy" not in pub.headers
    assert r.json()["type"] == "image"
    assert r.json()["markdown"] == f"[s.png]({public_url})"
    assert r.json()["artifactUri"] == public_url
    assert metadata.json()["artifactUri"] == public_url
    assert c.get("/api/artifacts/nope/public").status_code == 404
    bad = c.post("/guilds/art1/artifacts", files={"file": ("s.png", b"zz", "image/png")}, headers=h)
    assert bad.status_code == 415
    assert (
        c.post("/guilds/art1/artifacts", files={"file": ("s.png", PNG, "image/png")}).status_code
        == 401
    )


def test_task_upload_persists_and_broadcasts_link(client, monkeypatch):
    from routes import artifacts

    monkeypatch.setenv("FRONTEND_URL", "https://pioneer.example/")
    c, db = client
    insert_guild(db, "art-task")
    insert_member(db, "art-task", "m-art-task", "member")
    conversation_id = insert_conversation(db, "art-task")
    insert_task(db, "art-task", "t-art-task", conversation_id=conversation_id)
    h = {"Authorization": f"Bearer {make_auth_token(db, user_id='m-art-task')}"}
    broadcasts = []

    async def fake_store(data, ctype, guild, task_id):
        return artifact_service.StoredObject(
            "report-id", "report.html", "u", ctype, len(data), "html"
        )

    async def broadcast(guild, message):
        broadcasts.append((guild, message))

    monkeypatch.setattr(artifact_service, "store", fake_store)
    monkeypatch.setattr(artifacts, "broadcast_msg", broadcast)
    r = c.post(
        "/guilds/art-task/artifacts",
        files={"file": ("report.html", b"<h1>report</h1>", "text/html")},
        data={"task_id": "t-art-task"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    public_url = "https://pioneer.example/api/artifacts/report-id/public"
    assert r.json()["public_url"] == public_url
    metadata = c.get("/guilds/art-task/artifacts/report-id", headers=h)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["public_url"] == public_url

    class Body:
        def iter_chunks(self, size):
            yield b"<h1>report</h1>"

    monkeypatch.setattr(artifact_service, "open_object", lambda key: Body())
    downloaded = c.get(public_url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"<h1>report</h1>"
    log = c.get("/api/task/t-art-task/log", headers=h)
    assert log.status_code == 200, log.text
    assert [entry["line"] for entry in log.json()["logs"]] == [
        f"Artifact: [report.html]({public_url}?type=html)"
    ]
    assert len(broadcasts) == 1
    guild, message = broadcasts[0]
    assert guild == "art-task"
    assert message.type == "terminal-output" and message.taskId == "t-art-task"
    assert message.line == log.json()["logs"][0]["line"]
    assert message.timestamp
    messages = c.get(f"/api/guilds/art-task/conversations/{conversation_id}/messages", headers=h)
    assert messages.status_code == 200, messages.text
    assert [m["content"] for m in messages.json()] == [message.line]
    assert messages.json()[0]["taskId"] == "t-art-task"

    insert_guild(db, "other-art")
    insert_task(db, "other-art", "t-other-art")
    rejected = c.post(
        "/guilds/art-task/artifacts",
        files={"file": ("report.html", b"<h1>report</h1>", "text/html")},
        data={"task_id": "t-other-art"},
        headers=h,
    )
    assert rejected.status_code == 404
    assert len(broadcasts) == 1


@pytest.mark.parametrize("with_task", [False, True])
def test_worker_artifact_api_returns_public_url(client, monkeypatch, with_task):
    c, db = client
    guild_id = f"art-worker-{with_task}"
    worker_id = f"w-{guild_id}"
    artifact_id = f"report-{guild_id}"
    task_id = f"t-{guild_id}" if with_task else None
    insert_guild(db, guild_id)
    insert_worker(db, guild_id, worker_id)
    with _sync_session(db) as session:
        worker = session.get(Worker, worker_id)
        worker.auth_token = f"token-{worker_id}"
        session.commit()
    if task_id:
        insert_task(db, guild_id, task_id)
    headers = {"Authorization": f"Bearer token-{worker_id}"}
    monkeypatch.setenv("FRONTEND_URL", "https://pioneer.example/")

    async def fake_store(data, ctype, guild, task):
        assert guild == guild_id and task == task_id
        return artifact_service.StoredObject(
            artifact_id, f"{artifact_id}.txt", "legacy-storage-url", ctype, len(data), "text"
        )

    monkeypatch.setattr(artifact_service, "store", fake_store)
    # Same multipart request used by pioneer_worker.artifact_upload.ArtifactSync.
    uploaded = c.post(
        f"/guilds/{guild_id}/artifacts",
        headers=headers,
        files={"file": ("report.txt", b"worker report", "text/plain")},
        data={"task_id": task_id} if task_id else {},
    )
    assert uploaded.status_code == 200, uploaded.text
    public_url = f"https://pioneer.example/api/artifacts/{artifact_id}/public"
    assert uploaded.json()["public_url"] == public_url
    assert uploaded.json()["uploadedByWorkerId"] == worker_id
    assert uploaded.json()["taskId"] == task_id
    metadata = c.get(f"/guilds/{guild_id}/artifacts/{artifact_id}", headers=headers)
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["public_url"] == public_url

    class Body:
        def iter_chunks(self, size):
            yield b"worker report"

    monkeypatch.setattr(artifact_service, "open_object", lambda key: Body())
    downloaded = c.get(public_url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"worker report"


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
