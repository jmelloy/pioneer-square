"""Per-task child context for the standalone Foreman.

A ``TaskContext`` is a lightweight, isolated conversation thread that owns the
foreman's review loop for a single already-assigned task. It runs as its own
asyncio task inside the foreman process — not a separate OS process or WebSocket
connection — and reuses the parent's ``ForemanHTTPClient`` and ``ws_send``.

Isolation comes from two narrowings applied by ``run_foreman_ai(child=True)``:
history is loaded filtered by ``task_id``, and the system prompt / state preamble
/ tool set are scoped to this one task. See docs/foreman-per-task-context.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .runner import run_foreman_ai

if TYPE_CHECKING:
    from .config import Config
    from .http_client import ForemanHTTPClient

logger = logging.getLogger(__name__)

_CHILD_QUEUE_MAX = 50

# States that mean the underlying task is closed and the child should stop.
_TERMINAL_TASK_STATES = frozenset({"done", "failed", "cancelled"})


class TaskContext:
    """Isolated conversation thread for one task, driven by a serial queue.

    Triggers routed by the parent are enqueued via :meth:`enqueue` and consumed
    one at a time by :meth:`run`. Each trigger runs a child-scoped foreman turn.
    When the child calls ``finalize_task`` it signals teardown via ``on_finalize``.
    """

    def __init__(
        self,
        task_id: str,
        *,
        guild_id: str,
        user_id: str | None,
        http: ForemanHTTPClient,
        ws_send: Callable[[dict], Awaitable[None]],
        config: Config,
        on_finalize: Callable[[str], None] | None = None,
    ):
        self.task_id = task_id
        self._guild_id = guild_id
        self._user_id = user_id
        self._http = http
        self._ws_send = ws_send
        self._config = config
        self._on_finalize = on_finalize
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_CHILD_QUEUE_MAX)
        self._task: asyncio.Task | None = None
        # True once finalize_task has fired for this task — the loop drains its
        # current item and then exits.
        self._finalized = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the consumer loop as an asyncio task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=f"foreman.child:{self.task_id}")

    def is_alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def enqueue(self, trigger: dict) -> bool:
        """Queue a trigger for this task. Returns False if the queue is full."""
        try:
            self._queue.put_nowait(trigger)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "child %s: queue full (%d); dropping event=%s",
                self.task_id,
                _CHILD_QUEUE_MAX,
                trigger.get("event", "?"),
            )
            return False

    async def stop(self) -> None:
        """Cancel the consumer loop and wait for it to unwind."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── consumer loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Drain triggers one at a time until finalized or cancelled."""
        logger.info("child %s: started", self.task_id)
        try:
            while True:
                trigger = await self._queue.get()
                try:
                    await self._handle(trigger)
                except Exception:
                    logger.exception("child %s: error handling trigger", self.task_id)
                finally:
                    self._queue.task_done()
                if self._finalized:
                    logger.info("child %s: finalized — exiting loop", self.task_id)
                    break
        except asyncio.CancelledError:
            logger.debug("child %s: cancelled", self.task_id)
            raise
        finally:
            if self._on_finalize is not None:
                self._on_finalize(self.task_id)

    async def _handle(self, trigger: dict) -> None:
        human_message = trigger.get("human_message", "")
        if not human_message:
            return
        await run_foreman_ai(
            self._guild_id,
            human_message,
            extra_context=trigger.get("extra_context", ""),
            user_id=trigger.get("user_id") or self._user_id,
            task_id=self.task_id,
            http=self._http,
            ws_send=self._ws_send,
            config=self._config,
            child=True,
            tool_observer=self._observe_tool,
        )

    # ── tool observation ──────────────────────────────────────────────────────

    def _observe_tool(self, tool_name: str, tool_input: dict, result: dict) -> None:
        """Detect terminal tool calls so the child can tear itself down.

        ``finalize_task`` / ``cancel_task`` close the task; once one succeeds the
        loop exits after the current turn.
        """
        if result.get("is_error"):
            return
        if tool_name in ("finalize_task", "cancel_task"):
            tid = tool_input.get("task_id") or self.task_id
            if tid == self.task_id:
                logger.info("child %s: %s observed — scheduling teardown", self.task_id, tool_name)
                self._finalized = True
