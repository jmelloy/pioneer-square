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
