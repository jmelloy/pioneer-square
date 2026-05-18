"""Standalone foreman process — Phase 2 of the foreman extraction.

Connects to the backend WebSocket as an external foreman agent, receives
``foreman-trigger`` events, and calls ``run_foreman_ai()`` locally.  Broadcast
calls made by the AI loop are relayed back through the backend connection so
frontend clients receive them normally.

Usage (direct):
    BACKEND_WS_URL=ws://localhost:8000 GUILD_ID=abc123 python foreman/main.py

Usage (docker compose):
    GUILD_ID=abc123 docker compose --profile foreman up foreman

Environment variables:
    BACKEND_WS_URL   WebSocket base URL of the backend  (default: ws://backend:8000)
    GUILD_ID         Guild this foreman instance manages (required)
    DB_PATH          Path to the SQLite database file   (default: pioneer_square.db)
    DATABASE_URL     Full SQLAlchemy URL; overrides DB_PATH
    ANTHROPIC_API_KEY  Anthropic key for the AI loop
    LOG_LEVEL        Logging level                       (default: INFO)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# ── Python path ──────────────────────────────────────────────────────────────
# The foreman entry point lives at <repo>/foreman/main.py while all backend
# modules (database, models, events, foreman/runner, …) live under
# <repo>/backend/.  Insert backend/ so those imports resolve without any
# packaging changes to the backend.
_BACKEND_DIR = Path(__file__).parent.parent / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

# ── Patch broadcast BEFORE importing runner / tools ──────────────────────────
# runner.py and tools.py do `from events import broadcast` at module load time,
# binding the *function object*.  We install _broadcast_override on the events
# module so that every call to broadcast() — regardless of where it was imported
# — goes through our relay path.  The override must be set before the first
# import of runner or tools.
import events as _events  # noqa: E402  (must precede runner import)

_relay_ws: websockets.WebSocketClientProtocol | None = None

logger = logging.getLogger("pioneer_foreman")


async def _relay_broadcast(guild_id: str, message: dict, exclude: object = None) -> None:
    """Relay a broadcast message to the backend for fan-out to frontend clients."""
    ws = _relay_ws
    if ws is None:
        return
    try:
        await ws.send(
            json.dumps(
                {
                    "type": "foreman-broadcast",
                    "guildId": guild_id,
                    "payload": message,
                }
            )
        )
    except Exception as exc:
        logger.warning("relay_broadcast failed: %s", exc)


_events._broadcast_override = _relay_broadcast

# Safe to import the runner now; every broadcast() call will hit _relay_broadcast.
import websockets  # noqa: E402
from foreman.runner import run_foreman_ai  # noqa: E402
from util.tasks import spawn  # noqa: E402

# ── Trigger handling ──────────────────────────────────────────────────────────


async def _handle_trigger(data: dict) -> None:
    guild_id = data.get("guildId", "")
    human_message = data.get("humanMessage", "")
    user_id = data.get("userId")
    if not guild_id or not human_message:
        logger.warning("Ignoring malformed foreman-trigger: %s", data)
        return
    spawn(
        run_foreman_ai(guild_id, human_message, user_id=user_id),
        name=f"foreman.trigger:{guild_id}:{data.get('event', '?')}",
    )


# ── WebSocket connection loop ─────────────────────────────────────────────────


async def _run_guild(backend_ws_url: str, guild_id: str) -> None:
    """Connect to the backend WS for *guild_id* and handle triggers until evicted."""
    global _relay_ws

    ws_url = f"{backend_ws_url.rstrip('/')}/ws/{guild_id}"
    logger.info("Connecting to backend at %s", ws_url)

    async with websockets.connect(ws_url) as ws:
        _relay_ws = ws
        try:
            # Register as an external foreman — the backend checks `external: true`
            # to distinguish us from the legacy browser join that also sends
            # agentType="foreman".
            await ws.send(
                json.dumps(
                    {
                        "type": "join",
                        "agentType": "foreman",
                        "external": True,
                    }
                )
            )

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type")
                if msg_type == "foreman-registered":
                    logger.info(
                        "Registered as external foreman: agentId=%s guild=%s",
                        msg.get("agentId"),
                        guild_id,
                    )
                elif msg_type == "foreman-trigger":
                    logger.debug(
                        "Received foreman-trigger: event=%s guild=%s",
                        msg.get("event"),
                        guild_id,
                    )
                    await _handle_trigger(msg)
                elif msg_type == "foreman-evicted":
                    logger.warning(
                        "Evicted from guild %s: %s — reconnecting",
                        guild_id,
                        msg.get("reason"),
                    )
                    break
                # All other message types (broadcasts the backend sends to this
                # connection, etc.) are intentionally ignored here.
        finally:
            _relay_ws = None


# ── Entrypoint ────────────────────────────────────────────────────────────────


async def main() -> None:
    backend_ws_url = os.environ.get("BACKEND_WS_URL", "ws://backend:8000")
    guild_id = os.environ.get("GUILD_ID", "")

    if not guild_id:
        logger.error(
            "GUILD_ID environment variable is required — "
            "set it to the 6-char guild ID shown in the Pioneer Square UI"
        )
        sys.exit(1)

    retry_delay = 5
    while True:
        try:
            await _run_guild(backend_ws_url, guild_id)
            # Clean exit (e.g. evicted) — reset the backoff
            retry_delay = 5
        except Exception as exc:
            logger.error(
                "Connection error for guild %s: %s — retrying in %ds",
                guild_id,
                exc,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    asyncio.run(main())
