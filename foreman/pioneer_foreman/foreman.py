"""Standalone Foreman process: WebSocket connection, trigger handling, poll loop."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import websockets

from .http_client import ForemanHTTPClient
from .runner import run_foreman_ai

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)


class Foreman:
    """Manages the standalone foreman lifecycle for one guild."""

    def __init__(self, config: Config):
        self._config = config
        self._http: ForemanHTTPClient | None = None

    async def run(self) -> None:
        """Connect to the backend and handle triggers until stopped."""
        self._http = ForemanHTTPClient(
            self._config.http_url,
            self._config.guild_id,
            backend_key=self._config.backend_key,
            auth_token=self._config.auth_token,
        )
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
                        retry_delay = 5
                except Exception as exc:
                    logger.error(
                        "Connection error for guild %s: %s — retrying in %ds",
                        self._config.guild_id,
                        exc,
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
        finally:
            if self._http:
                await self._http.close()

    async def _run_connection(self) -> bool:
        """Connect once; return True if evicted, False on clean disconnect."""
        ws_url = self._config.ws_url
        logger.info("Connecting to backend at %s", ws_url)
        evicted = False
        poll_task: asyncio.Task | None = None

        async with websockets.connect(ws_url) as ws:

            async def _ws_send(message: dict) -> None:
                try:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "foreman-broadcast",
                                "guildId": self._config.guild_id,
                                "payload": message,
                            }
                        )
                    )
                except Exception as exc:
                    logger.warning("_ws_send failed: %s", exc)

            async def _handle_trigger(data: dict) -> None:
                guild_id = data.get("guildId", "")
                human_message = data.get("humanMessage", "")
                user_id = data.get("userId")
                extra_context = data.get("extraContext", "")
                if not guild_id or not human_message:
                    logger.warning("Ignoring malformed foreman-trigger: %s", data)
                    return
                asyncio.create_task(
                    run_foreman_ai(
                        guild_id,
                        human_message,
                        extra_context=extra_context,
                        user_id=user_id,
                        http=self._http,
                        ws_send=_ws_send,
                        config=self._config,
                    ),
                    name=f"foreman.trigger:{guild_id}:{data.get('event', '?')}",
                )

            async def _poll_loop() -> None:
                """Background poll — check active tasks periodically."""
                interval = self._config.poll_min_interval
                while True:
                    try:
                        await asyncio.sleep(interval)
                    except asyncio.CancelledError:
                        return

                    try:
                        state = await self._http.get_state()
                        active = [
                            t
                            for t in (state.get("tasks") or [])
                            if t.get("state") not in ("done", "failed", "cancelled")
                        ]
                        n = len(active)
                        next_interval = min(interval * 2, self._config.poll_max_interval)

                        if active:
                            task_summary = "; ".join(f"{t['id']} ({t['state']})" for t in active)
                            msg = (
                                f"[periodic-check] Automated status poll — {n} non-terminal "
                                f"task(s): {task_summary}. Check whether any are stalled."
                            )
                            asyncio.create_task(
                                run_foreman_ai(
                                    self._config.guild_id,
                                    msg,
                                    http=self._http,
                                    ws_send=_ws_send,
                                    config=self._config,
                                ),
                                name=f"foreman.poll:{self._config.guild_id}",
                            )

                        interval = next_interval
                        await _ws_send(
                            {
                                "type": "foreman-poll-status",
                                "nextCheckIn": interval,
                            }
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("poll_loop iteration failed")

            # Register as external foreman
            await ws.send(
                json.dumps(
                    {
                        "type": "join",
                        "agentType": "foreman",
                        "external": True,
                    }
                )
            )

            poll_task = asyncio.create_task(_poll_loop(), name="foreman.poll-loop")
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
                            self._config.guild_id,
                        )
                    elif msg_type == "foreman-trigger":
                        logger.debug(
                            "Received foreman-trigger: event=%s",
                            msg.get("event"),
                        )
                        await _handle_trigger(msg)
                    elif msg_type == "foreman-evicted":
                        logger.warning("Evicted: %s", msg.get("reason"))
                        evicted = True
                        break
            finally:
                if poll_task and not poll_task.done():
                    poll_task.cancel()
                    try:
                        await poll_task
                    except asyncio.CancelledError:
                        pass

        return evicted
