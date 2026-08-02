"""Sync Discord application slash commands to the Discord API.

Called automatically from ``main.py``'s lifespan on startup so that command
definitions stay in sync with the deployed codebase without requiring a
manual ``scripts/register_discord_commands.py`` run after every deploy.

Uses a bulk-overwrite ``PUT`` (idempotent). Skipped silently when
``DISCORD_APPLICATION_ID`` or ``DISCORD_BOT_TOKEN`` are absent so the server
boots normally in environments without Discord configured.

Set ``DISCORD_DEV_GUILD_ID`` to register to a specific guild instead of
globally (guild-scoped commands propagate instantly; global ones take up to
one hour).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

# Canonical command definitions. The standalone helper script
# ``scripts/register_discord_commands.py`` contains a copy — keep them in sync
# when adding or changing commands.
COMMANDS: list[dict] = [
    {
        "name": "ps",
        "description": "Pioneer Square control commands",
        "options": [
            {
                "name": "status",
                "description": "Show current workers and active task counts",
                "type": 1,  # SUB_COMMAND
            },
            {
                "name": "workers",
                "description": "List all workers with their state, repos, and agent count",
                "type": 1,
            },
            {
                "name": "pickup",
                "description": "Claim a GitHub issue and assign it to an idle worker",
                "type": 1,
                "options": [
                    {
                        "name": "issue-url",
                        "description": "Full GitHub issue URL (e.g. https://github.com/owner/repo/issues/123)",
                        "type": 3,  # STRING
                        "required": True,
                    }
                ],
            },
            {
                "name": "review",
                "description": "Trigger a PR review task",
                "type": 1,
                "options": [
                    {
                        "name": "pr-url",
                        "description": "Full GitHub PR URL (e.g. https://github.com/owner/repo/pull/456)",
                        "type": 3,
                        "required": True,
                    }
                ],
            },
            {
                "name": "cancel",
                "description": "Cancel a running task",
                "type": 1,
                "options": [
                    {
                        "name": "task-id",
                        "description": "Task ID to cancel (e.g. t-abc123)",
                        "type": 3,
                        "required": True,
                    }
                ],
            },
        ],
    },
    {
        "name": "join-channel",
        "description": "Wire this (or a chosen) Discord channel to a Pioneer Square guild",
        "options": [
            {
                "name": "channel",
                "description": "Discord channel to wire up",
                "type": 7,  # CHANNEL
                "required": True,
            },
            {
                "name": "guild",
                "description": "Pioneer Square guild slug (optional if only one guild is configured)",
                "type": 3,
                "required": False,
            },
        ],
    },
    {
        "name": "leave-channel",
        "description": "Remove a Discord channel's Pioneer Square guild binding",
        "options": [
            {
                "name": "channel",
                "description": "Discord channel to unwire (defaults to the current channel)",
                "type": 7,
                "required": False,
            },
        ],
    },
    {
        "name": "connect-account",
        "description": "Link your Discord account to your Pioneer Square account",
        "options": [],
    },
    {
        "name": "worker-spawn",
        "description": "Spawn a new Pioneer Square worker (requires a connected account)",
        "options": [
            {
                "name": "repos",
                "description": "Comma-separated owner/repo list (e.g. jmelloy/pioneer-square)",
                "type": 3,
                "required": False,
            },
            {
                "name": "tools",
                "description": "Comma-separated tools/agents (e.g. claude,codex)",
                "type": 3,
                "required": False,
            },
        ],
    },
]


def _do_sync(application_id: str, bot_token: str, guild_id: str = "") -> list[dict]:
    """Bulk-overwrite slash commands via Discord REST API (synchronous).

    Returns the list of registered command objects. Raises ``urllib.error.HTTPError``
    on a non-2xx response.
    """
    if guild_id:
        url = f"{DISCORD_API}/applications/{application_id}/guilds/{guild_id}/commands"
    else:
        url = f"{DISCORD_API}/applications/{application_id}/commands"

    data = json.dumps(COMMANDS).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (pioneer-square, 1.0)",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


async def sync_slash_commands_on_startup() -> None:
    """Sync slash commands at startup; skip silently if credentials are absent."""
    application_id = os.environ.get("DISCORD_APPLICATION_ID", "")
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    guild_id = os.environ.get("DISCORD_DEV_GUILD_ID", "")

    if not application_id or not bot_token:
        logger.debug(
            "Discord slash command sync skipped"
            " (DISCORD_APPLICATION_ID or DISCORD_BOT_TOKEN not set)"
        )
        return

    scope = f"guild {guild_id}" if guild_id else "global"
    logger.info("Syncing Discord slash commands (%s) …", scope)
    try:
        registered = await asyncio.to_thread(_do_sync, application_id, bot_token, guild_id)
        logger.info(
            "Synced %d Discord slash command(s): %s",
            len(registered),
            ", ".join(f"/{c['name']}" for c in registered),
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        logger.warning("Discord slash command sync failed: HTTP %d — %s", exc.code, detail)
    except Exception:
        logger.warning("Discord slash command sync failed", exc_info=True)
