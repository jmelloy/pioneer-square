"""Standalone foreman process — Phase 3 of the foreman extraction.

Connects to the backend WebSocket as an external foreman agent, receives
``foreman-trigger`` events, and calls ``run_foreman_ai()`` using REST for all
state I/O.  No direct database access — everything goes through the backend API.

Usage (direct):
    pioneer-foreman                                  # reads ./pioneer-foreman.toml
    pioneer-foreman --config /etc/foreman.toml
    pioneer-foreman --backend-url ws://localhost:8000 --guild-id abc123

Usage (docker compose):
    PIONEER_GUILD_ID=abc123 docker compose --profile pioneer-foreman up pioneer-foreman

Environment variables (override TOML values):
    PIONEER_BACKEND_URL        Backend URL (ws:// or http://)
    PIONEER_GUILD_ID           Guild this foreman manages (required)
    PIONEER_FOREMAN_AUTH_TOKEN Pre-configured worker auth_token (skip re-registration)
    ANTHROPIC_API_KEY          Anthropic key for the AI loop (required)
    FOREMAN_MODEL              Claude model  (default: claude-sonnet-4-6)
    PIONEER_GITHUB_TOKEN       GitHub OAuth token (fetched from backend if absent)
    GITHUB_TOKEN               Alternative GitHub token env var
    LOG_LEVEL                  Logging level (default: INFO)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from dataclasses import dataclass

import httpx
import websockets

from pioneer_foreman import __version__
from pioneer_foreman.config import load
from pioneer_foreman.runner import reset_foreman_poll, run_foreman_ai

logger = logging.getLogger("pioneer_foreman")


@dataclass
class _RuntimeState:
    """Mutable runtime state kept separate from the immutable Config."""

    auth_token: str | None = None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def _register(http: httpx.AsyncClient, guild_id: str) -> str:
    """Register as a worker to obtain an auth_token for REST calls.

    Returns the auth_token string.
    """
    hostname = socket.gethostname()
    resp = await http.post(
        f"/guilds/{guild_id}/workers",
        json={"repos": [], "hostname": f"foreman@{hostname}"},
    )
    resp.raise_for_status()
    data = resp.json()
    token: str = data["auth_token"]
    logger.info(
        "Registered as worker %s (foreman identity) for guild %s",
        data.get("id", "?"),
        guild_id,
    )
    return token


# ---------------------------------------------------------------------------
# WebSocket connection loop
# ---------------------------------------------------------------------------


async def _run_guild(cfg, state: _RuntimeState) -> bool:
    """Connect to the backend WS and handle foreman triggers until evicted.

    Returns True if evicted (caller should back off), False for other exits.
    """
    guild_id = cfg.guild_id

    # Bare httpx client without auth for the registration step (no auth needed
    # for POST /guilds/{guild_id}/workers — it's an open endpoint).
    async with httpx.AsyncClient(base_url=cfg.http_url, timeout=30.0) as anon_http:
        if state.auth_token:
            auth_token = state.auth_token
        elif cfg.auth_token:
            auth_token = cfg.auth_token
            state.auth_token = auth_token
        else:
            auth_token = await _register(anon_http, guild_id)
            state.auth_token = auth_token  # cache for reconnects within this process lifetime

    # Authenticated client — all subsequent REST calls use this.
    async with httpx.AsyncClient(
        base_url=cfg.http_url,
        headers={"Authorization": f"Bearer {auth_token}"},
        timeout=30.0,
    ) as http:
        ws_url = cfg.ws_url
        logger.info("Connecting to backend at %s", ws_url)
        evicted = False

        async with websockets.connect(ws_url) as ws:

            async def _relay_broadcast(relay_guild_id: str, message: dict, **_) -> None:
                try:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "foreman-broadcast",
                                "guildId": relay_guild_id,
                                "payload": message,
                            }
                        )
                    )
                except Exception as exc:
                    logger.warning("relay_broadcast failed: %s", exc)

            # Register as an external foreman
            await ws.send(
                json.dumps(
                    {
                        "type": "join",
                        "agentType": "foreman",
                        "external": True,
                    }
                )
            )

            # Start background poll loop
            reset_foreman_poll(
                guild_id,
                http=http,
                broadcast_fn=_relay_broadcast,
                github_token=cfg.github_token,
                model=cfg.model,
                poll_interval=cfg.poll_interval,
            )

            try:
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
                        human_message = msg.get("humanMessage", "")
                        event = msg.get("event", "?")
                        user_id = msg.get("userId")
                        logger.debug("Received foreman-trigger: event=%s guild=%s", event, guild_id)
                        if not human_message:
                            logger.warning("Ignoring foreman-trigger with no humanMessage")
                            continue

                        # Reset poll so the next check happens in poll_interval seconds
                        reset_foreman_poll(
                            guild_id,
                            http=http,
                            broadcast_fn=_relay_broadcast,
                            github_token=cfg.github_token,
                            model=cfg.model,
                            poll_interval=cfg.poll_interval,
                        )

                        asyncio.create_task(
                            run_foreman_ai(
                                guild_id,
                                human_message,
                                user_id=user_id,
                                http=http,
                                broadcast_fn=_relay_broadcast,
                                github_token=cfg.github_token,
                                model=cfg.model,
                            ),
                            name=f"foreman.trigger:{guild_id}:{event}",
                        )

                    elif msg_type == "foreman-evicted":
                        logger.warning(
                            "Evicted from guild %s: %s",
                            guild_id,
                            msg.get("reason", "no reason given"),
                        )
                        evicted = True
                        break

                    # All other message types are silently ignored.

            finally:
                # Cancel poll loop when the WS connection closes
                from pioneer_foreman.runner import _poll_tasks

                task = _poll_tasks.pop(guild_id, None)
                if task and not task.done():
                    task.cancel()

    return evicted


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _run(cfg) -> None:
    retry_delay = 5
    state = _RuntimeState(auth_token=None)
    while True:
        try:
            evicted = await _run_guild(cfg, state)
            if evicted:
                logger.info("Evicted; waiting %ds before reconnecting", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            else:
                retry_delay = 5
        except Exception as exc:
            logger.error(
                "Connection error for guild %s: %s — retrying in %ds",
                cfg.guild_id,
                exc,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pioneer Square standalone foreman agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to pioneer-foreman.toml (default: ./pioneer-foreman.toml)",
    )
    parser.add_argument(
        "--backend-url",
        metavar="URL",
        help="Backend URL (ws:// or http://), overrides config/env",
    )
    parser.add_argument(
        "--guild-id",
        metavar="ID",
        help="Guild ID to manage, overrides config/env",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        help="Claude model to use (default: claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging level (default: INFO)",
    )
    parser.add_argument("--version", action="version", version=f"pioneer-foreman {__version__}")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    overrides: dict = {}
    if args.backend_url:
        overrides["backend_url"] = args.backend_url
    if args.guild_id:
        overrides["guild_id"] = args.guild_id
    if args.model:
        overrides["model"] = args.model

    try:
        cfg = load(args.config, overrides=overrides)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set — the foreman AI loop cannot run without it.")
        sys.exit(1)

    logger.info(
        "pioneer-foreman %s starting: guild=%s backend=%s model=%s",
        __version__,
        cfg.guild_id,
        cfg.http_url,
        cfg.model,
    )

    asyncio.run(_run(cfg))


if __name__ == "__main__":
    main()
