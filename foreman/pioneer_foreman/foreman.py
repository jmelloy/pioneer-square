"""Standalone Foreman process: WebSocket connection, trigger handling.

Periodic task-health polling is scheduled by the backend (see
backend/foreman/runner.py's ``_poll_loop``), which dispatches each tick as a
``foreman-trigger`` to whichever foreman — embedded or external — currently
owns the guild. This process only reacts to triggers it receives over the WS
connection; it does not run its own poll timer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
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
        # Keys with a parent (whole-guild) run currently in flight. A trigger for
        # a key that's already running is dropped rather than buffered; the backend
        # poll/re-trigger mechanism recovers it on the next tick. See
        # backend/foreman/runner.py:run_foreman_ai for the matching embedded policy.
        self._active_runs: set[tuple[str, str | None]] = set()
        # Per-task locks serialising per-task child-context runs (task_id -> Lock).
        # Mirrors backend/foreman/runner.py's _guild_locks: if a task's lock is
        # already held, the incoming trigger is dropped rather than queued or
        # buffered — the next lifecycle event or poll tick will retry.  See
        # docs/foreman-per-task-context.md.
        self._task_locks: dict[str, asyncio.Lock] = {}
        # Per-connection broadcast closure; set in _run_connection, cleared on exit.
        self._ws_send: Callable[[dict], Awaitable[None]] | None = None

    # ── per-task child contexts ────────────────────────────────────────────

    async def _run_task_trigger(
        self,
        guild_id: str,
        human_message: str,
        extra_context: str,
        user_id: str | None,
        task_id: str,
    ) -> None:
        """Run one child-scoped foreman turn for ``task_id``, serialised by a
        per-task lock. Drops the trigger if a run for this task is already
        in flight instead of queuing it (see docs/foreman-per-task-context.md).
        """
        lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            logger.info(
                "guild=%s task=%s: dropping concurrent invocation (already running)",
                guild_id,
                task_id,
            )
            return
        await lock.acquire()
        try:
            await run_foreman_ai(
                guild_id,
                human_message,
                extra_context=extra_context,
                user_id=user_id,
                task_id=task_id,
                http=self._http,
                ws_send=self._ws_send,
                config=self._config,
                child=True,
            )
        except Exception:
            logger.exception("guild=%s task=%s: unhandled error in child run", guild_id, task_id)
        finally:
            lock.release()
            self._task_locks.pop(task_id, None)

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
                        "check guild_id and backend_key. Retrying in %ds. (%s)",
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
            if self._http:
                await self._http.close()

    async def _run_connection(self) -> bool:
        """Connect once; return True if evicted, False on clean disconnect."""
        ws_url = self._config.ws_url
        logger.info("Connecting to backend at %s", ws_url)
        evicted = False

        async with websockets.connect(ws_url) as ws:
            logger.info("WebSocket connected to %s", ws_url)

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

            # Expose the broadcast closure to child contexts spawned this connection.
            self._ws_send = _ws_send

            async def _run_foreman(
                guild_id: str,
                human_message: str,
                *,
                extra_context: str = "",
                user_id: str | None = None,
                task_id: str | None = None,
                task_name: str = "foreman.run",
            ) -> None:
                """Run one parent (whole-guild) foreman turn.

                Drop-if-busy: if a run is already in flight for this (guild, user) key,
                log and drop the trigger rather than buffering it — the backend's
                poll/re-trigger mechanism recovers it on the next tick. Matches
                backend/foreman/runner.py:run_foreman_ai (the embedded foreman's policy).
                """
                key = (guild_id, user_id)
                if key in self._active_runs:
                    if "[periodic-check]" in human_message:
                        logger.debug(
                            "guild=%s %s: dropping periodic-check — run already in-flight",
                            guild_id,
                            task_name,
                        )
                    else:
                        logger.info(
                            "guild=%s %s: dropped — run already in-flight",
                            guild_id,
                            task_name,
                        )
                    return
                self._active_runs.add(key)
                try:
                    await run_foreman_ai(
                        guild_id,
                        human_message,
                        extra_context=extra_context,
                        user_id=user_id,
                        task_id=task_id,
                        http=self._http,
                        ws_send=_ws_send,
                        config=self._config,
                    )
                except Exception:
                    logger.exception("guild=%s %s: unhandled error", guild_id, task_name)
                finally:
                    self._active_runs.discard(key)

            async def _handle_trigger(data: dict) -> None:
                guild_id = data.get("guildId", "")
                human_message = data.get("humanMessage", "")
                user_id = data.get("userId")
                extra_context = data.get("extraContext", "")
                trigger_task_id = data.get("taskId")
                if not guild_id or not human_message:
                    logger.warning("Ignoring malformed foreman-trigger: %s", data)
                    return

                # Task-scoped triggers run in an isolated, per-task child context
                # serialised by a per-task lock rather than the whole-guild queue
                # below. See docs/foreman-per-task-context.md.
                if self._config.child_contexts and trigger_task_id:
                    asyncio.create_task(
                        self._run_task_trigger(
                            guild_id, human_message, extra_context, user_id, trigger_task_id
                        ),
                        name=f"foreman.task:{trigger_task_id}",
                    )
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

            # Register as external foreman
            logger.debug("guild=%s sending join message", self._config.guild_id)
            await ws.send(
                json.dumps(
                    {
                        "type": "join",
                        "agentType": "foreman",
                        "external": True,
                    }
                )
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
                            "Registered as external foreman: agentId=%s guild=%s",
                            msg.get("agentId"),
                            self._config.guild_id,
                        )
                    elif msg_type == "foreman-trigger":
                        logger.debug(
                            "Received foreman-trigger: event=%s",
                            msg.get("event"),
                        )
                        try:
                            await _handle_trigger(msg)
                        except Exception:
                            logger.exception(
                                "guild=%s _handle_trigger raised — event=%s",
                                self._config.guild_id,
                                msg.get("event"),
                            )
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
                self._ws_send = None

        return evicted
