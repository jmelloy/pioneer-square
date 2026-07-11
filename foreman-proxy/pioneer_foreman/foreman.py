"""Standalone Foreman proxy WebSocket lifecycle.

This process does not run the Foreman conversation loop. The backend owns
triggers, state, history, tools, and locking. The proxy holds one WebSocket
connection and answers ``foreman-api-request`` messages by executing one LLM
API call against the operator-selected provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import websockets

from .runner import close_clients, run_api_request

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


class Foreman:
    """Manages the standalone Foreman API proxy lifecycle for one guild."""

    def __init__(self, config: Config):
        self._config = config
        self._inflight: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        """Connect to the backend and proxy API requests until stopped."""
        retry_delay = 5
        try:
            while True:
                try:
                    evicted = await self._run_connection()
                    if evicted:
                        logger.info("Evicted; waiting %ds before reconnecting", retry_delay)
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 60)
                    else:
                        logger.info(
                            "guild=%s WS disconnected cleanly — reconnecting in %ds",
                            self._config.guild_id,
                            retry_delay,
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay = 5
                except (ConnectionRefusedError, OSError) as exc:
                    logger.error(
                        "guild=%s cannot reach backend at %s — is it running? "
                        "Retrying in %ds. (%s: %s)",
                        self._config.guild_id,
                        self._config.ws_url,
                        retry_delay,
                        type(exc).__name__,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                except websockets.exceptions.InvalidHandshake as exc:
                    logger.error(
                        "guild=%s backend rejected WS handshake at %s — "
                        "check guild_id. Retrying in %ds. (%s)",
                        self._config.guild_id,
                        self._config.ws_url,
                        retry_delay,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                except Exception as exc:
                    logger.exception(
                        "guild=%s unexpected connection error at %s — retrying in %ds: %s",
                        self._config.guild_id,
                        self._config.ws_url,
                        retry_delay,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
        finally:
            await close_clients()

    async def _run_connection(self) -> bool:
        """Connect once; return True if evicted, False on clean disconnect."""
        ws_url = self._config.ws_url
        logger.info("Connecting to backend at %s", ws_url)
        evicted = False
        send_lock = asyncio.Lock()

        async with websockets.connect(ws_url) as ws:
            logger.info("WebSocket connected to %s", ws_url)

            async def _send(message: dict) -> None:
                async with send_lock:
                    await ws.send(json.dumps(message))

            async def _handle_api_request(data: dict) -> None:
                request_id = data.get("requestId")
                if not request_id:
                    logger.warning("Ignoring malformed foreman-api-request: %s", data)
                    return
                try:
                    result = await run_api_request(data, self._config)
                    await _send(
                        {
                            "type": "foreman-api-response",
                            "requestId": request_id,
                            "guildId": self._config.guild_id,
                            "ok": True,
                            **result,
                        }
                    )
                except Exception as exc:
                    logger.exception("foreman-api-request failed requestId=%s", request_id)
                    await _send(
                        {
                            "type": "foreman-api-response",
                            "requestId": request_id,
                            "guildId": self._config.guild_id,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

            def _spawn_api_request(data: dict) -> None:
                task = asyncio.create_task(
                    _handle_api_request(data),
                    name=f"foreman-api-request:{data.get('requestId', '?')}",
                )
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)

            logger.debug("guild=%s sending join message", self._config.guild_id)
            await _send(
                {
                    "type": "join",
                    "agentType": "foreman",
                    "agentName": "Foreman API Proxy",
                    "external": True,
                }
            )

            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON WS frame (ignoring): %.120r", raw)
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "foreman-registered":
                        logger.info(
                            "Registered as external Foreman API proxy: agentId=%s guild=%s",
                            msg.get("agentId"),
                            self._config.guild_id,
                        )
                    elif msg_type == "foreman-api-request":
                        _spawn_api_request(msg)
                    elif msg_type == "foreman-evicted":
                        logger.warning("Evicted: %s", msg.get("reason"))
                        evicted = True
                        break
                    else:
                        logger.debug("Ignoring WS message type=%r", msg_type)
                logger.info(
                    "guild=%s WS message loop ended (evicted=%s)",
                    self._config.guild_id,
                    evicted,
                )
            finally:
                for task in list(self._inflight):
                    task.cancel()
                if self._inflight:
                    await asyncio.gather(*self._inflight, return_exceptions=True)
                self._inflight.clear()

        return evicted
