"""Artifact sync debounces edits, retains sources, and retries failed uploads."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pioneer_worker import artifact_upload


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_upload, "artifact_dir", lambda task_id: tmp_path)
    clock = [0.0]
    monkeypatch.setattr(artifact_upload, "monotonic", lambda: clock[0])
    sent = []
    responses = [200, None]

    def handler(req):
        body = req.content.decode("latin-1")
        name = body.split('filename="', 1)[1].split('"', 1)[0]
        ctype = body.split("Content-Type: ", 1)[1].split("\r\n", 1)[0]
        sent.append((name, ctype, body))
        if responses[1]:
            responses[1]()
        return httpx.Response(responses[0], json={"id": str(len(sent))})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return clock, sent, responses


def sync():
    return artifact_upload.ArtifactSync("http://b", "g", "tok", "t-1")


async def test_debounce_uploads_reports_and_images_without_deleting(tmp_path, uploads):
    clock, sent, _ = uploads
    for name in ("shot.png", "report.html", "notes.md", "data.bin"):
        (tmp_path / name).write_bytes(b"x")
    uploader = sync()
    await uploader.scan()
    clock[0] = 4.9
    await uploader.scan()
    assert sent == []
    clock[0] = 5
    await uploader.scan()
    assert sorted((name, ctype) for name, ctype, _ in sent) == [
        ("notes.md", "text/markdown"),
        ("report.html", "text/html"),
        ("shot.png", "image/png"),
    ]
    assert all((tmp_path / name).exists() for name in ("shot.png", "report.html", "notes.md"))
    await uploader.scan()
    assert len(sent) == 3


async def test_edits_reset_debounce_and_publish_new_content(tmp_path, uploads):
    clock, sent, _ = uploads
    p = tmp_path / "report.html"
    p.write_text("v1")
    uploader = sync()
    await uploader.scan()
    clock[0] = 4
    p.write_text("version two")
    await uploader.scan()
    clock[0] = 5
    await uploader.scan()
    assert sent == []
    clock[0] = 9
    await uploader.scan()
    assert len(sent) == 1 and "version two" in sent[0][2]
    p.write_text("version three")
    await uploader.scan()
    clock[0] = 14
    await uploader.scan()
    assert len(sent) == 2 and "version three" in sent[1][2]
    assert p.exists()


async def test_hashes_skip_unchanged_content_across_followups(tmp_path, uploads):
    clock, sent, _ = uploads
    p = tmp_path / "notes.md"
    p.write_text("same content")
    uploader = sync()
    await uploader.scan()
    clock[0] = 5
    await uploader.scan()
    # A new sync instance models a follow-up or worker restart.
    p.touch()
    uploader = sync()
    await uploader.scan()
    clock[0] = 10
    await uploader.scan()
    assert len(sent) == 1


async def test_failed_upload_retries_without_losing_source(tmp_path, uploads):
    clock, sent, responses = uploads
    p = tmp_path / "notes.md"
    p.write_text("keep me")
    uploader = sync()
    await uploader.scan()
    clock[0] = 5
    responses[0] = 503
    await uploader.scan()
    assert p.exists()
    responses[0] = 200
    await uploader.scan()
    assert len(sent) == 2
    await uploader.scan()
    assert len(sent) == 2


async def test_edit_during_upload_is_synced_again(tmp_path, uploads):
    clock, sent, responses = uploads
    p = tmp_path / "notes.md"
    p.write_text("first")

    def edit():
        if len(sent) == 1:
            p.write_text("changed during request")

    responses[1] = edit
    uploader = sync()
    await uploader.scan()
    clock[0] = 5
    await uploader.scan()
    await uploader.scan()
    clock[0] = 10
    await uploader.scan()
    assert len(sent) == 2 and "changed during request" in sent[1][2]
    assert p.read_text() == "changed during request"


async def test_watcher_flushes_last_file_after_quiet_period(tmp_path, uploads, monkeypatch):
    clock, sent, _ = uploads
    (tmp_path / "notes.md").write_text("last report")

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(artifact_upload.asyncio, "sleep", sleep)
    stop = asyncio.Event()
    stop.set()
    await sync().watch(stop)
    assert clock[0] >= 5 and len(sent) == 1


async def test_final_upload_retains_and_resyncs_an_edit_during_request(
    tmp_path, uploads, monkeypatch
):
    clock, sent, responses = uploads
    p = tmp_path / "notes.md"
    p.write_text("first")

    def edit():
        if len(sent) == 1:
            p.write_text("changed during upload")

    async def sleep(seconds):
        clock[0] += seconds

    responses[1] = edit
    monkeypatch.setattr(artifact_upload.asyncio, "sleep", sleep)
    uploader = sync()
    await uploader.scan()
    clock[0] = 5
    stop = asyncio.Event()
    stop.set()
    await uploader.watch(stop)
    assert len(sent) == 2 and "changed during upload" in sent[1][2]
    assert p.exists()


async def test_oversized_file_is_retained_but_not_uploaded(tmp_path, uploads, monkeypatch):
    clock, sent, _ = uploads
    monkeypatch.setattr(artifact_upload, "MAX_BYTES", 2)
    p = tmp_path / "notes.md"
    p.write_text("too big")
    uploader = sync()
    await uploader.scan()
    clock[0] = 5
    await uploader.scan()
    assert sent == [] and p.exists()


async def test_watcher_uploads_before_stopping(tmp_path, uploads, monkeypatch):
    _, sent, _ = uploads
    (tmp_path / "notes.md").write_text("live report")
    monkeypatch.setattr(artifact_upload, "DEBOUNCE_SECONDS", 0)
    stop = asyncio.Event()
    watcher = asyncio.create_task(sync().watch(stop))
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if sent:
                break
        assert len(sent) == 1 and not watcher.done()
    finally:
        stop.set()
        await watcher
