"""Fire-and-forget Discord webhook notifier.

Usage::

    import discord_notifier
    await discord_notifier.notify("task-complete", "Task done", "t-abc finished")

Set ``DISCORD_WEBHOOK_URL`` in the environment to enable.  When the variable is
unset every call is a silent no-op.  HTTP failures are logged as warnings and
never propagate — the caller is never affected.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Colour palette for Discord embed sidebar
_COLOURS: dict[str, int] = {
    "task-complete": 0x2ECC71,  # green
    "task-failed": 0xE74C3C,  # red
    "task-cancelled": 0xE74C3C,  # red
    "pr-opened": 0x3498DB,  # blue
    "pr-merged": 0x9B59B6,  # purple
    "pr-closed": 0x95A5A6,  # grey
    "worker-online": 0x1ABC9C,  # teal
    "worker-offline": 0xE67E22,  # orange
    "ci-pass": 0x2ECC71,  # green
    "ci-fail": 0xE74C3C,  # red
}
_DEFAULT_COLOUR = 0x7289DA  # blurple


def _webhook_url() -> str | None:
    return os.environ.get("DISCORD_WEBHOOK_URL") or None


async def notify(
    event_type: str,
    title: str,
    description: str,
    url: str | None = None,
    color: int | None = None,
) -> None:
    """Post a Discord embed to the configured webhook.

    Silent no-op when ``DISCORD_WEBHOOK_URL`` is not set.
    Never raises — HTTP errors are logged at WARNING level.
    """
    webhook_url = _webhook_url()
    if not webhook_url:
        return

    resolved_color = color if color is not None else _COLOURS.get(event_type, _DEFAULT_COLOUR)

    embed: dict = {
        "title": title,
        "description": description,
        "color": resolved_color,
    }
    if url:
        embed["url"] = url

    payload = {"embeds": [embed]}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
    except Exception:
        logger.warning("Discord webhook notify failed (event=%s)", event_type, exc_info=True)
