#!/usr/bin/env python3
"""Register (or update) Pioneer Square slash commands with Discord.

Run once after adding or changing commands:

    DISCORD_APPLICATION_ID=<id> DISCORD_BOT_TOKEN=<token> python scripts/register_discord_commands.py

To register commands for a specific Discord guild only (faster for dev):

    DISCORD_APPLICATION_ID=<id> DISCORD_BOT_TOKEN=<token> \
        DISCORD_DEV_GUILD_ID=<guild_id> python scripts/register_discord_commands.py

Without DISCORD_DEV_GUILD_ID the commands are registered globally (takes up to 1 hour to propagate).
"""

import json
import os
import sys
import urllib.error
import urllib.request

APPLICATION_ID = os.environ.get("DISCORD_APPLICATION_ID", "")
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DEV_GUILD_ID = os.environ.get("DISCORD_DEV_GUILD_ID", "")

if not APPLICATION_ID or not BOT_TOKEN:
    print("Error: DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN must be set.", file=sys.stderr)
    sys.exit(1)

# /ps command with five subcommands
COMMANDS = [
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
                "type": 1,  # SUB_COMMAND
            },
            {
                "name": "pickup",
                "description": "Claim a GitHub issue and assign it to an idle worker",
                "type": 1,  # SUB_COMMAND
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
                "type": 1,  # SUB_COMMAND
                "options": [
                    {
                        "name": "pr-url",
                        "description": "Full GitHub PR URL (e.g. https://github.com/owner/repo/pull/456)",
                        "type": 3,  # STRING
                        "required": True,
                    }
                ],
            },
            {
                "name": "cancel",
                "description": "Cancel a running task",
                "type": 1,  # SUB_COMMAND
                "options": [
                    {
                        "name": "task-id",
                        "description": "Task ID to cancel (e.g. t-abc123)",
                        "type": 3,  # STRING
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
                "type": 3,  # STRING
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
                "type": 7,  # CHANNEL
                "required": False,
            },
        ],
    },
    {
        "name": "connect-account",
        "description": "Link your Discord account to your Pioneer Square account",
        "options": [],
    },
]

DISCORD_API = "https://discord.com/api/v10"

if DEV_GUILD_ID:
    url = f"{DISCORD_API}/applications/{APPLICATION_ID}/guilds/{DEV_GUILD_ID}/commands"
    scope = f"guild {DEV_GUILD_ID}"
else:
    url = f"{DISCORD_API}/applications/{APPLICATION_ID}/commands"
    scope = "global"

print(f"Registering commands ({scope}) …")

data = json.dumps(COMMANDS).encode()
req = urllib.request.Request(
    url,
    data=data,
    method="PUT",
    headers={
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
    },
)

try:
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    print(f"Registered {len(body)} command(s):")
    for cmd in body:
        print(f"  /{cmd['name']} — {cmd['description']}")
    print("Done.")
except urllib.error.HTTPError as exc:
    detail = exc.read().decode(errors="replace")
    print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
    sys.exit(1)
