"""Sync the per-task artifact folder while an agent runs; retain files for later edits.

The upload endpoint persists and broadcasts each artifact link into the task log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import tempfile
from pathlib import Path
from time import monotonic
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Mirrors backend artifact_service.ALLOWED_TYPES.
ARTIFACT_EXTS = set(".png .jpg .jpeg .webp .gif .pdf .html .htm .txt .md .csv .json".split())
mimetypes.add_type("text/markdown", ".md")  # not in every platform's mime table
MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10
DEBOUNCE_SECONDS = 5


def artifact_dir(task_id: str) -> Path:
    return Path(tempfile.gettempdir()) / "pioneer-artifacts" / task_id


def prompt_note(task_id: str) -> str:
    d = artifact_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return (
        "\n\nSCREENSHOTS & REPORTS: if you capture screenshots or images (e.g. from browser "
        "automation) or produce a report worth sharing, save it as a PNG/JPG/WebP/GIF image, "
        "a self-contained HTML page, or a PDF/TXT/MD/CSV/JSON file (max 10MB, 10 files) in "
        f"{d}. This folder is watched: after a file is unchanged for 5 seconds it is uploaded "
        "and linked in the task log. Files stay on disk; editing one publishes a new version "
        "after another 5-second quiet period. Do not put artifact:// links in commit messages "
        "or PR descriptions; GitHub cannot resolve them."
    )


class ArtifactSync:
    def __init__(self, http_url: str, guild_id: str, auth_token: str | None, task_id: str):
        self.directory = artifact_dir(task_id)
        self.url = f"{http_url.rstrip('/')}/guilds/{quote(guild_id, safe='')}/artifacts"
        self.auth_token = auth_token or ""
        self.task_id = task_id
        self.observed: dict[str, tuple[tuple[int, int, int], float]] = {}
        self.synced: dict[str, tuple[int, int, int]] = {}
        self.state_path = self.directory / ".uploaded-hashes"
        try:
            hashes = json.loads(self.state_path.read_text())
            if not isinstance(hashes, dict):
                raise ValueError("Expected an artifact hash mapping")
            self.hashes = hashes
        except (OSError, ValueError):
            self.hashes = {}

    async def scan(self) -> bool:
        """Upload changed, quiet files. Return whether any files are still settling."""
        pending = False
        try:
            files = sorted(
                p
                for p in self.directory.iterdir()
                if p.is_file() and p.suffix.lower() in ARTIFACT_EXTS
            )[:MAX_FILES]
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning("Artifact scan %s failed: %s", self.task_id, exc)
            return False
        async with httpx.AsyncClient(timeout=60) as client:
            for p in files:
                try:
                    stat = p.stat()
                    signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                    previous, changed_at = self.observed.get(p.name, (None, monotonic()))
                    if previous != signature:
                        changed_at = monotonic()
                    self.observed[p.name] = (signature, changed_at)
                    if self.synced.get(p.name) == signature:
                        continue
                    if monotonic() - changed_at < DEBOUNCE_SECONDS:
                        pending = True
                        continue
                    if stat.st_size > MAX_BYTES:
                        logger.warning("Artifact %s exceeds size limit; skipped", p.name)
                        continue
                    with p.open("rb") as source:
                        data = source.read(MAX_BYTES + 1)
                    after = p.stat()
                    after_signature = (after.st_mtime_ns, after.st_ctime_ns, after.st_size)
                    if after_signature != signature:
                        self.observed[p.name] = (after_signature, monotonic())
                        pending = True
                        continue
                    if len(data) > MAX_BYTES:
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    if self.hashes.get(p.name) != digest:
                        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                        resp = await client.post(
                            self.url,
                            headers={"Authorization": f"Bearer {self.auth_token}"},
                            files={"file": (p.name, data, ctype)},
                            data={"task_id": self.task_id},
                        )
                        if resp.status_code != 200:
                            logger.warning(
                                "Artifact upload %s: HTTP %s %s",
                                p.name,
                                resp.status_code,
                                resp.text[:200],
                            )
                            continue
                        hashes = {**self.hashes, p.name: digest}
                        # Persist hashes across follow-ups/restarts without touching source files.
                        temporary = self.state_path.with_suffix(".tmp")
                        temporary.write_text(json.dumps(hashes))
                        temporary.replace(self.state_path)
                        self.hashes = hashes
                    self.synced[p.name] = signature
                    after = p.stat()
                    after_signature = (after.st_mtime_ns, after.st_ctime_ns, after.st_size)
                    if after_signature != signature:
                        self.observed[p.name] = (after_signature, monotonic())
                        pending = True
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    logger.warning("Artifact upload %s failed: %s", p.name, exc)
        return pending

    async def watch(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.scan()
            try:
                async with asyncio.timeout(1):
                    await stop.wait()
            except TimeoutError:
                pass
        # The last report may have been written immediately before the agent exited.
        while await self.scan():
            await asyncio.sleep(DEBOUNCE_SECONDS)
