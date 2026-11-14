"""Keep inline image bytes out of conversation and terminal payloads."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import UTC, datetime

import artifact_service
from models import Artifact
from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)
_DATA_IMAGE = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)")


async def externalize_images(
    payload: dict, db: AsyncSession, guild_id: str, guild_pk: int, task_id: str | None
) -> tuple[dict, bool]:
    """Return a link-only payload and whether it is safe to replace the stored original.

    Claude nests images in JSON-string tool outputs; Pi also sends structured blocks.
    Failed conversions must not overwrite historical image data.
    """
    complete = True
    links: dict[tuple[str, str], str] = {}

    async def image_link(data: str, content_type: str) -> str:
        nonlocal complete
        key = (data, content_type)
        if key in links:
            return links[key]
        try:
            cfg = artifact_service.load_config()
            limit = cfg.max_bytes if cfg else artifact_service.MAX_ARTIFACT_BYTES_DEFAULT
            if len(data) > ((limit + 2) // 3) * 4:
                raise artifact_service.ArtifactError(
                    "Inline image exceeds artifact size limit", 413
                )
            raw = base64.b64decode(data, validate=True)
            obj = await artifact_service.store(raw, content_type, guild_id, task_id)
            db.add(
                Artifact(
                    id=obj.id,
                    guild_id=guild_pk,
                    key=obj.key,
                    url=obj.url,
                    content_type=obj.content_type,
                    size_bytes=obj.size_bytes,
                    task_id=task_id,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            link = f"[image](artifact://{obj.id})"
        except (artifact_service.ArtifactError, ValueError, binascii.Error):
            logger.warning("Inline image could not be stored for guild %s", guild_id)
            complete = False
            link = "[image unavailable]"
        links[key] = link
        return link

    async def convert(value):
        if isinstance(value, dict):
            if value.get("type") == "image":
                source = value.get("source", value)
                if isinstance(source, dict) and isinstance(source.get("data"), str):
                    return {
                        "type": "text",
                        "text": await image_link(
                            source["data"], source.get("media_type") or source.get("mimeType", "")
                        ),
                    }
            return {key: await convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [await convert(item) for item in value]
        if isinstance(value, str):
            # Avoid parsing ordinary text or copying large image-free JSON logs.
            if "image" in value and "data" in value:
                starts = [i for ch in ("{", "[") if (i := value.find(ch)) >= 0]
                if starts:
                    start = min(starts)
                    try:
                        parsed, end = json.JSONDecoder().raw_decode(value[start:])
                    except (ValueError, RecursionError):
                        pass
                    else:
                        return (
                            value[:start]
                            + json.dumps(await convert(parsed), ensure_ascii=False)
                            + await convert(value[start + end :])
                        )
            matches = list(_DATA_IMAGE.finditer(value))
            for match in reversed(matches):
                link = await image_link(match[2], match[1])
                start, end = match.span()
                # Replace the whole Markdown image, not just its URL with nested Markdown.
                prefix = re.search(r"!?\[[^\]\n]*\]\($", value[:start])
                if prefix and value[end : end + 1] == ")":
                    start, end = prefix.start(), end + 1
                value = value[:start] + link + value[end:]
        return value

    return await convert(payload), complete


async def message_payloads(messages, db: AsyncSession, guild_id: str, guild_pk: int) -> list[dict]:
    """Lazily migrate legacy messages, leaving originals intact on storage failure."""
    from routes.guilds import _message_dict  # noqa: PLC0415

    payloads = []
    changed = False
    for message in messages:
        original = _message_dict(message)
        payload, complete = await externalize_images(
            original, db, guild_id, guild_pk, message.task_id
        )
        if complete and payload != original:
            message.content = payload["content"]
            if message.meta:
                message.meta = payload["meta"]
            changed = True
        payloads.append(payload)
    # Also commit artifact rows from partially successful conversions.
    if changed or db.new:
        await db.commit()
    return payloads
