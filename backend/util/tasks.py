"""Background-task helpers.

``asyncio.create_task`` returns a Task that the event loop only weakly
references — if the caller doesn't keep a strong reference, the task can be
garbage-collected mid-execution and any exception it raises is swallowed
with at most a "Task exception was never retrieved" message at GC time.

``spawn`` wraps ``create_task`` so the task is:

  1. retained in a module-level registry until it completes, and
  2. wrapped with a done-callback that logs any uncaught exception via
     ``logger.exception`` so silent failures stop being silent.

Use ``spawn(coro, name="...")`` instead of ``asyncio.create_task(coro)`` for
fire-and-forget background work (foreman triggers, WS-side broadcasts that
shouldn't block the handler, etc.).  The optional ``name`` is used both for
the asyncio task name and as the prefix on the failure log line.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Strong references prevent premature GC of running tasks.  Tasks remove
# themselves on completion via the done-callback below.
_pending: set[asyncio.Task[Any]] = set()


def _on_done(task: asyncio.Task[Any]) -> None:
    _pending.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception("Background task %s failed", task.get_name(), exc_info=exc)


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Schedule *coro* as a retained, error-logging background task."""
    task = asyncio.create_task(coro, name=name)
    _pending.add(task)
    task.add_done_callback(_on_done)
    return task


def pending_count() -> int:
    """Number of currently-tracked background tasks (for tests/diagnostics)."""
    return len(_pending)
