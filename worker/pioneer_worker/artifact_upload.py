"""Upload images and reports (HTML/text/PDF) an agent saved to the per-task artifact dir.

Agents are told (see ``prompt_note``) to save images under ``artifact_dir(task_id)``, which
lives outside the git worktree so it never dirties it. After the run the worker uploads each
file through the backend ``POST /guilds/{guild}/artifacts`` API (credentials stay on the
backend) and appends ``[name](artifact://<id>)`` markdown links to the task's completion message.
"""

from __future__ import annotations

import logging
import mimetypes
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Mirrors backend artifact_service.ALLOWED_TYPES.
ARTIFACT_EXTS = set(".png .jpg .jpeg .webp .gif .pdf .html .htm .txt .md .csv .json".split())
mimetypes.add_type("text/markdown", ".md")  # not in every platform's mime table
MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10


def artifact_dir(task_id: str) -> Path:
    return Path(tempfile.gettempdir()) / "pioneer-artifacts" / task_id


def prompt_note(task_id: str) -> str:
    d = artifact_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return (
        "\n\nSCREENSHOTS & REPORTS: if you capture screenshots or images (e.g. from browser "
        "automation) or produce a report worth sharing, save it as a PNG/JPG/WebP/GIF image, "
        "a self-contained HTML page, or a PDF/TXT/MD/CSV/JSON file (max 10MB) in "
        f"{d}. They are uploaded automatically and linked in your completion message as "
        "[name](artifact://<id>) markdown links. Do not put artifact:// links in "
        "commit messages or PR descriptions; GitHub cannot resolve them."
    )


async def upload_task_artifacts(
    http_url: str, guild_id: str, auth_token: str, task_id: str
) -> list[dict]:
    """Upload files in the task's artifact dir; return backend payloads. Never raises."""
    d = artifact_dir(task_id)
    if not d.is_dir():
        return []
    files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in ARTIFACT_EXTS)[
        :MAX_FILES
    ]
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


def append_markdown(text: str, artifacts: list[dict]) -> str:
    if not artifacts:
        return text
    links = "\n".join(a.get("markdown") or f"[artifact](artifact://{a['id']})" for a in artifacts)
    return f"{text}\n\n**Artifacts:**\n{links}".lstrip()
