"""Standalone Foreman process: WebSocket connection, trigger handling, poll loop."""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
        # In-flight lock: held while a foreman run coroutine is executing.
        # _run_foreman checks lock.locked() (no await between check and acquire,
        # so it is atomic in asyncio's cooperative scheduler) and drops the call
        # if the lock is already held — preventing concurrent runs per guild.
        self._in_flight: asyncio.Lock = asyncio.Lock()
        # Monotonic timestamp of the last run that made at least one tool call.
        # Used to decide whether to reset the poll backoff.
        self._last_action_at: float = 0.0
        # Current poll interval in seconds.  Reset to poll_min_interval when
        # the foreman makes tool calls; otherwise advanced exponentially each tick.
        self._poll_interval: int = config.poll_min_interval

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

            async def _run_foreman(
                guild_id: str,
                human_message: str,
                *,
                extra_context: str = "",
                user_id: str | None = None,
                task_id: str | None = None,
                task_name: str = "foreman.run",
            ) -> None:
                """Wrapper that enforces the in-flight lock and updates last_action_at."""
                if self._in_flight.locked():
                    logger.info(
                        "guild=%s %s: dropped — run already in-flight",
                        guild_id,
                        task_name,
                    )
                    return
                await self._in_flight.acquire()
                try:
                    made_calls = await run_foreman_ai(
                        guild_id,
                        human_message,
                        extra_context=extra_context,
                        user_id=user_id,
                        task_id=task_id,
                        http=self._http,
                        ws_send=_ws_send,
                        config=self._config,
                    )
                    if made_calls:
                        self._last_action_at = time.monotonic()
                        self._poll_interval = self._config.poll_min_interval
                except Exception:
                    logger.exception("guild=%s %s: unhandled error", guild_id, task_name)
                finally:
                    self._in_flight.release()

            async def _handle_trigger(data: dict) -> None:
                guild_id = data.get("guildId", "")
                human_message = data.get("humanMessage", "")
                user_id = data.get("userId")
                extra_context = data.get("extraContext", "")
                trigger_task_id = data.get("taskId")
                if not guild_id or not human_message:
                    logger.warning("Ignoring malformed foreman-trigger: %s", data)
                    return
                asyncio.create_task(
                    _run_foreman(
                        guild_id,
                        human_message,
                        extra_context=extra_context,
                        user_id=user_id,
                        task_id=trigger_task_id,
                        task_name=f"foreman.trigger:{guild_id}:{data.get('event', '?')}",
                    ),
                    name=f"foreman.trigger:{guild_id}:{data.get('event', '?')}",
                )

            async def _poll_loop() -> None:
                """Background poll — check active tasks periodically."""
                while True:
                    # Capture the sleep duration so we can detect if a run reset
                    # self._poll_interval during the sleep.
                    sleep_duration = self._poll_interval
                    try:
                        await asyncio.sleep(sleep_duration)
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

                        # Advance the interval for the next tick.  If a triggered run
                        # reset self._poll_interval to poll_min_interval during our
                        # sleep, that value will be smaller than sleep_duration, so
                        # we advance from the reset value (not from sleep_duration).
                        base = min(self._poll_interval, sleep_duration)
                        self._poll_interval = min(base * 2, self._config.poll_max_interval)

                        if active and not self._in_flight.locked():
                            task_summary = "; ".join(f"{t['id']} ({t['state']})" for t in active)
                            msg = (
                                f"[periodic-check] Automated status poll — {n} non-terminal "
                                f"task(s): {task_summary}. Check whether any are stalled."
                            )
                            asyncio.create_task(
                                _run_foreman(
                                    self._config.guild_id,
                                    msg,
                                    task_name=f"foreman.poll:{self._config.guild_id}",
                                ),
                                name=f"foreman.poll:{self._config.guild_id}",
                            )

                        await _ws_send(
                            {
                                "type": "foreman-poll-status",
                                "nextCheckIn": self._poll_interval,
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
