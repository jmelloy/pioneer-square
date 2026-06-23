"""Standalone Foreman process: WebSocket connection, trigger handling, poll loop."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import websockets

from .http_client import ForemanHTTPClient
from .runner import run_foreman_ai
from .task_context import _TERMINAL_TASK_STATES, TaskContext

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

_QUEUE_MAX = 100

# Matches a task id (e.g. "t-abc123") in an assign_task tool result like
# "Task t-abc123 assigned to w-1." / "Task t-abc123 queued for w-1."
_TASK_ID_RE = re.compile(r"\bt-[a-z0-9]+\b")


class Foreman:
    """Manages the standalone foreman lifecycle for one guild."""

    def __init__(self, config: Config):
        self._config = config
        self._http: ForemanHTTPClient | None = None
        # True while a foreman run (including its queue drain) is executing.
        self._processing: bool = False
        # Triggers buffered while _processing is True; drained FIFO after each turn.
        self._message_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAX)
        # Monotonic timestamp of the last run that made at least one tool call.
        # Used to decide whether to reset the poll backoff.
        self._last_action_at: float = 0.0
        # Current poll interval in seconds.  Reset to poll_min_interval when
        # the foreman makes tool calls; otherwise advanced exponentially each tick.
        self._poll_interval: int = config.poll_min_interval
        # Per-task child contexts (task_id → TaskContext), populated on assign_task
        # when config.child_contexts is enabled.  See docs/foreman-per-task-context.md.
        self._child_contexts: dict[str, TaskContext] = {}
        # Triggers that arrived for a task between assign_task and the child being
        # registered; flushed into the child's queue on spawn.
        self._pending_for_task: dict[str, list[dict]] = {}
        # Per-connection broadcast closure; set in _run_connection, cleared on exit.
        self._ws_send: Callable[[dict], Awaitable[None]] | None = None

    # ── per-task child contexts ────────────────────────────────────────────

    @staticmethod
    def _normalize_trigger(data: dict) -> dict:
        """Convert a foreman-trigger WS payload into the internal trigger dict
        shared by the parent queue and child queues."""
        return {
            "guild_id": data.get("guildId", ""),
            "human_message": data.get("humanMessage", ""),
            "extra_context": data.get("extraContext", ""),
            "user_id": data.get("userId"),
            "task_id": data.get("taskId"),
            "event": data.get("event", "?"),
        }

    def _route_to_child(self, trigger: dict) -> bool:
        """Route a task-specific trigger to its child context if one exists or is
        pending. Returns True if the trigger was consumed by a child, False if the
        parent should handle it."""
        if not self._config.child_contexts:
            return False
        task_id = trigger.get("task_id")
        if not task_id:
            return False
        child = self._child_contexts.get(task_id)
        if child is not None and child.is_alive():
            child.enqueue(trigger)
            return True
        # Assignment in flight — buffer until the child is registered.
        if task_id in self._pending_for_task:
            self._pending_for_task[task_id].append(trigger)
            return True
        return False

    def _spawn_child(self, task_id: str) -> None:
        """Create and start a child context for an assigned task, then flush any
        buffered pre-spawn triggers into it. Idempotent."""
        if not self._config.child_contexts or self._ws_send is None or self._http is None:
            return
        if task_id in self._child_contexts and self._child_contexts[task_id].is_alive():
            return
        child = TaskContext(
            task_id,
            guild_id=self._config.guild_id,
            user_id=None,
            http=self._http,
            ws_send=self._ws_send,
            config=self._config,
            on_finalize=self._deregister_child,
        )
        self._child_contexts[task_id] = child
        child.start()
        logger.info("spawned child context for task %s", task_id)
        for buffered in self._pending_for_task.pop(task_id, []):
            child.enqueue(buffered)

    def _deregister_child(self, task_id: str) -> None:
        """Remove a finalized/dead child from the routing map (called by the child)."""
        self._child_contexts.pop(task_id, None)
        self._pending_for_task.pop(task_id, None)
        logger.debug("deregistered child context for task %s", task_id)

    def _parent_tool_observer(self, tool_name: str, tool_input: dict, result: dict) -> None:
        """React to the parent's task lifecycle tool calls: spawn a child on
        assign_task, tear one down if the parent finalizes/cancels a task itself."""
        if result.get("is_error"):
            return
        if tool_name == "assign_task":
            content = result.get("content") or ""
            match = _TASK_ID_RE.search(content)
            task_id = match.group(0) if match else tool_input.get("task_id")
            if not task_id:
                logger.warning("assign_task succeeded but no task_id found in result: %r", content)
                return
            # Open the pre-spawn buffer before starting the child so events arriving
            # during spawn are captured, then create and flush the child.
            self._pending_for_task.setdefault(task_id, [])
            self._spawn_child(task_id)
        elif tool_name in ("finalize_task", "cancel_task"):
            task_id = tool_input.get("task_id")
            child = self._child_contexts.get(task_id) if task_id else None
            if child is not None:
                logger.info("parent %s task %s — tearing down its child", tool_name, task_id)
                asyncio.create_task(child.stop(), name=f"foreman.child-stop:{task_id}")

    async def _respawn_children_from_state(self) -> None:
        """On (re)connect, spawn a child for every non-terminal task with a worker.

        History is DB-backed, so each child reconstructs its own context on first
        turn. A synthetic [reconnect] trigger nudges it to re-check the task."""
        if not self._config.child_contexts or self._http is None:
            return
        try:
            state = await self._http.get_state()
            for t in state.get("tasks") or []:
                task_id = t.get("id")
                if not task_id or t.get("state") in _TERMINAL_TASK_STATES or not t.get("worker_id"):
                    continue
                if task_id in self._child_contexts and self._child_contexts[task_id].is_alive():
                    continue
                self._spawn_child(task_id)
                child = self._child_contexts.get(task_id)
                if child is not None:
                    child.enqueue(
                        {
                            "guild_id": self._config.guild_id,
                            "human_message": (
                                f"[reconnect] Foreman reconnected; task {task_id} is in state "
                                f"'{t.get('state')}'. Check whether it still needs action."
                            ),
                            "extra_context": "",
                            "user_id": None,
                            "task_id": task_id,
                            "event": "reconnect",
                        }
                    )
        except Exception:
            logger.exception("respawn: failed to re-establish child contexts")

    async def _cancel_all_children(self) -> None:
        """Cancel every child context (on disconnect/eviction)."""
        children = list(self._child_contexts.values())
        self._child_contexts.clear()
        self._pending_for_task.clear()
        for child in children:
            await child.stop()

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
        poll_task: asyncio.Task | None = None

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
                """Run one foreman turn, then drain any buffered messages in FIFO order.

                Holds _processing=True for the entire duration (initial turn + drain) so
                that triggers arriving during the drain are queued rather than dispatched.
                """
                if self._processing:
                    logger.info(
                        "guild=%s %s: dropped — run already in-flight",
                        guild_id,
                        task_name,
                    )
                    return
                self._processing = True
                try:
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
                            tool_observer=self._parent_tool_observer,
                        )
                        if made_calls:
                            self._last_action_at = time.monotonic()
                            self._poll_interval = self._config.poll_min_interval
                    except Exception:
                        logger.exception("guild=%s %s: unhandled error", guild_id, task_name)

                    # Drain buffered messages in FIFO order.  _processing stays True so
                    # any triggers arriving during draining are queued, not dispatched.
                    while not self._message_queue.empty():
                        try:
                            queued = self._message_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        q_name = f"foreman.queued:{queued['guild_id']}:{queued.get('event', '?')}"
                        logger.debug("Processing queued message: %s", q_name)
                        try:
                            made_calls = await run_foreman_ai(
                                queued["guild_id"],
                                queued["human_message"],
                                extra_context=queued.get("extra_context", ""),
                                user_id=queued.get("user_id"),
                                task_id=queued.get("task_id"),
                                http=self._http,
                                ws_send=_ws_send,
                                config=self._config,
                                tool_observer=self._parent_tool_observer,
                            )
                            if made_calls:
                                self._last_action_at = time.monotonic()
                                self._poll_interval = self._config.poll_min_interval
                        except Exception:
                            logger.exception(
                                "guild=%s %s: unhandled error", queued["guild_id"], q_name
                            )
                finally:
                    self._processing = False

            async def _handle_trigger(data: dict) -> None:
                guild_id = data.get("guildId", "")
                human_message = data.get("humanMessage", "")
                user_id = data.get("userId")
                extra_context = data.get("extraContext", "")
                trigger_task_id = data.get("taskId")
                if not guild_id or not human_message:
                    logger.warning("Ignoring malformed foreman-trigger: %s", data)
                    return

                # Route task-specific events to their isolated child context.
                if self._route_to_child(self._normalize_trigger(data)):
                    logger.debug(
                        "Routed trigger to child context: task=%s event=%s",
                        trigger_task_id,
                        data.get("event", "?"),
                    )
                    return

                if self._processing:
                    # Periodic-check messages re-fire on the next poll interval; discard them.
                    if "[periodic-check]" in human_message:
                        logger.debug(
                            "Discarding periodic-check trigger while busy: guild=%s", guild_id
                        )
                        return
                    # Buffer all other triggers; drop with a warning if the queue is full.
                    try:
                        self._message_queue.put_nowait(
                            {
                                "guild_id": guild_id,
                                "human_message": human_message,
                                "extra_context": extra_context,
                                "user_id": user_id,
                                "task_id": trigger_task_id,
                                "event": data.get("event", "?"),
                            }
                        )
                        logger.debug(
                            "Buffered trigger while busy (queue=%d): guild=%s event=%s",
                            self._message_queue.qsize(),
                            guild_id,
                            data.get("event", "?"),
                        )
                    except asyncio.QueueFull:
                        logger.warning(
                            "Message queue full (%d); dropping trigger for guild=%s event=%s",
                            _QUEUE_MAX,
                            guild_id,
                            data.get("event", "?"),
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

                        # Orphan recovery + ownership split: when child contexts are
                        # enabled, each assigned task is monitored by its own child, so
                        # the parent's periodic check only covers tasks WITHOUT a live
                        # child (unassigned/pending or orphaned). Spawn a child for any
                        # assigned task missing one.
                        if self._config.child_contexts:
                            for t in active:
                                tid = t.get("id")
                                if (
                                    tid
                                    and t.get("worker_id")
                                    and not (
                                        tid in self._child_contexts
                                        and self._child_contexts[tid].is_alive()
                                    )
                                ):
                                    self._spawn_child(tid)
                            active = [
                                t
                                for t in active
                                if not (
                                    t.get("id") in self._child_contexts
                                    and self._child_contexts[t["id"]].is_alive()
                                )
                            ]
                        n = len(active)

                        # Advance the interval for the next tick.  If a triggered run
                        # reset self._poll_interval to poll_min_interval during our
                        # sleep, that value will be smaller than sleep_duration, so
                        # we advance from the reset value (not from sleep_duration).
                        base = min(self._poll_interval, sleep_duration)
                        self._poll_interval = min(base * 2, self._config.poll_max_interval)

                        if active and not self._processing:
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

            poll_task = asyncio.create_task(_poll_loop(), name="foreman.poll-loop")
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
                        # Re-establish per-task child contexts for any tasks that
                        # were already in flight (e.g. after a reconnect).
                        await self._respawn_children_from_state()
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
                if poll_task and not poll_task.done():
                    poll_task.cancel()
                    try:
                        await poll_task
                    except asyncio.CancelledError:
                        pass
                # Child contexts hold this connection's ws_send; tear them down so
                # the next connection respawns them cleanly from state.
                await self._cancel_all_children()
                self._ws_send = None

        return evicted
