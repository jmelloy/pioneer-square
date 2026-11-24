"""Upload images and reports (HTML/text/PDF) an agent saved to the per-task artifact dir.

Agents are told (see ``prompt_note``) to save images under ``artifact_dir(task_id)``, which
lives outside the git worktree so it never dirties it. After the run the worker uploads each
file through the backend ``POST /guilds/{guild}/artifacts`` API (credentials stay on the
backend). ``watch_task_artifacts`` does this while the agent runs, posting each upload as its own
log line so it renders inline in the task view; the completion message still lists them too.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Mirrors backend artifact_service.ALLOWED_TYPES.
ARTIFACT_EXTS = set(".png .jpg .jpeg .webp .gif .pdf .html .htm .txt .md .csv .json".split())
mimetypes.add_type("text/markdown", ".md")  # not in every platform's mime table
MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10
# A file touched this recently may still be mid-write; the next poll picks it up.
SETTLE_SECONDS = 2.0


def artifact_dir(task_id: str) -> Path:
    return Path(tempfile.gettempdir()) / "pioneer-artifacts" / task_id


def prompt_note(task_id: str) -> str:
    d = artifact_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return (
        "\n\nSCREENSHOTS & REPORTS: if you capture screenshots or images (e.g. from browser "
        "automation) or produce a report worth sharing, save it as a PNG/JPG/WebP/GIF image, "
        "a self-contained HTML page, or a PDF/TXT/MD/CSV/JSON file (max 10MB) in "
        f"{d}. They are uploaded automatically, shown in the task log, and linked in your "
        "completion message as "
        "[name](artifact://<id>) markdown links. Do not put artifact:// links in "
        "commit messages or PR descriptions; GitHub cannot resolve them."
    )


async def upload_task_artifacts(
    http_url: str,
    guild_id: str,
    auth_token: str,
    task_id: str,
    *,
    limit: int = MAX_FILES,
    min_age: float = 0.0,
) -> list[dict]:
    """Upload files in the task's artifact dir; return backend payloads. Never raises.

    Uploaded files are deleted, so repeated calls only pick up new files. ``min_age`` skips
    files modified in the last N seconds (possibly still being written).
    """
    d = artifact_dir(task_id)
    if not d.is_dir() or limit <= 0:
        return []
    cutoff = time.time() - min_age
    try:
        files = sorted(
            p
            for p in d.iterdir()
            if p.is_file() and p.suffix.lower() in ARTIFACT_EXTS and p.stat().st_mtime <= cutoff
        )[:limit]
    except OSError as exc:  # file vanished mid-scan; next poll retries
        logger.warning("Artifact scan of %s failed: %s", d, exc)
        return []
    out: list[dict] = []
    url = f"{http_url.rstrip('/')}/guilds/{quote(guild_id, safe='')}/artifacts"
    async with httpx.AsyncClient(timeout=60) as client:
        for p in files:
            try:
                if p.stat().st_size > MAX_BYTES:
                    logger.warning("Artifact %s exceeds size limit; skipped", p.name)
                    continue
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {auth_token}"},
                    files={"file": (p.name, p.read_bytes(), ctype)},
                    data={"task_id": task_id},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Artifact upload %s: HTTP %s %s", p.name, resp.status_code, resp.text[:200]
                    )
                    continue
                out.append(resp.json())
                p.unlink(missing_ok=True)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.warning("Artifact upload %s failed: %s", p.name, exc)
    return out


def artifact_markdown(a: dict) -> str:
    return a.get("markdown") or f"[artifact](artifact://{a['id']})"


async def watch_task_artifacts(
    http_url: str,
    guild_id: str,
    auth_token: str,
    task_id: str,
    emit: Callable[[str], Awaitable[None]],
    uploaded: list[dict],
    stop: asyncio.Event,
    interval: float = 5.0,
) -> None:
    """Upload artifacts as they appear until ``stop`` is set, then do a final sweep.

    Each upload is emitted as a markdown log line (rendered inline in the task view) and
    appended to ``uploaded`` for the completion message. Stop via the event, not cancel(),
    so an in-flight upload isn't lost.
    """
    while True:
        stopping = stop.is_set()
        batch = await upload_task_artifacts(
            http_url,
            guild_id,
            auth_token,
            task_id,
            limit=MAX_FILES - len(uploaded),
            min_age=0.0 if stopping else SETTLE_SECONDS,
        )
        for a in batch:
            uploaded.append(a)
            try:
                await emit(f"📎 {artifact_markdown(a)}")
            except Exception as exc:  # log transport hiccup must not kill the watcher
                logger.warning("Artifact emit failed: %s", exc)
        if stopping:
            return
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass


def append_markdown(text: str, artifacts: list[dict]) -> str:
    if not artifacts:
        return text
    links = "\n".join(artifact_markdown(a) for a in artifacts)
    return f"{text}\n\n**Artifacts:**\n{links}".lstrip()
