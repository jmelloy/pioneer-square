"""Worker artifact upload picks up images and report files, skipping everything else."""

from __future__ import annotations

import httpx
from pioneer_worker import artifact_upload


async def test_uploads_reports_and_images(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_upload, "artifact_dir", lambda task_id: tmp_path)
    for name in ("shot.png", "report.html", "notes.md", "data.bin"):
        (tmp_path / name).write_bytes(b"x")
    sent: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode("latin-1")
        name = body.split('filename="', 1)[1].split('"', 1)[0]
        ctype = body.split("Content-Type: ", 1)[1].split("\r\n", 1)[0]
        sent.append((name, ctype))
        return httpx.Response(200, json={"id": name, "markdown": f"[{name}](artifact://{name})"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    out = await artifact_upload.upload_task_artifacts("http://b", "g", "tok", "t-1")
    assert sorted(sent) == [
        ("notes.md", "text/markdown"),
        ("report.html", "text/html"),
        ("shot.png", "image/png"),
    ]
    assert len(out) == 3 and (tmp_path / "data.bin").exists()
    assert "**Artifacts:**" in artifact_upload.append_markdown("done", out)


async def test_watch_emits_each_upload_and_sweeps_on_stop(tmp_path, monkeypatch):
    import asyncio
    import os

    monkeypatch.setattr(artifact_upload, "artifact_dir", lambda task_id: tmp_path)
    (tmp_path / "early.png").write_bytes(b"x")
    os.utime(tmp_path / "early.png", (0, 0))  # settled
    (tmp_path / "fresh.png").write_bytes(b"x")  # still "mid-write" → waits for final sweep

    def handler(req: httpx.Request) -> httpx.Response:
        name = req.content.decode("latin-1").split('filename="', 1)[1].split('"', 1)[0]
        return httpx.Response(200, json={"id": name, "markdown": f"![{name}](artifact://{name})"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    lines: list[str] = []

    async def emit(line: str) -> None:
        lines.append(line)

    uploaded: list[dict] = []
    stop = asyncio.Event()
    task = asyncio.create_task(
        artifact_upload.watch_task_artifacts("http://b", "g", "tok", "t", emit, uploaded, stop, 60)
    )
    for _ in range(50):
        if lines:
            break
        await asyncio.sleep(0.01)
    assert lines == ["📎 ![early.png](artifact://early.png)"]
    stop.set()
    await task
    assert lines[-1] == "📎 ![fresh.png](artifact://fresh.png)"
    assert [a["id"] for a in uploaded] == ["early.png", "fresh.png"]
    assert not list(tmp_path.iterdir())
