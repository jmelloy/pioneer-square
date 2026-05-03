"""Tests for util.tasks.spawn — error logging and registry behaviour."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from util.tasks import pending_count, spawn  # noqa: E402


@pytest.fixture
def captured_util_tasks_logs():
    """Attach a record-collecting handler directly to the ``util.tasks`` logger.

    Avoids ``caplog``: pytest's logging plugin sets ``logger.disabled=True``
    between tests in a worker session, which suppresses emit *before* the
    record reaches caplog's root handler. We set ``disabled=False`` and
    pin a handler at the source so test ordering can't mask the assertion.
    """
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("util.tasks")
    handler = _ListHandler(level=logging.ERROR)
    prior_level = logger.level
    prior_disabled = logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.disabled = False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
        logger.disabled = prior_disabled


async def test_spawn_runs_and_clears_registry():
    ran = asyncio.Event()

    async def _job():
        ran.set()

    task = spawn(_job(), name="t")
    await task
    assert ran.is_set()
    # Done-callback runs as soon as the task finishes; give the loop one tick.
    await asyncio.sleep(0)
    assert pending_count() == 0


async def test_spawn_logs_uncaught_exception(captured_util_tasks_logs):
    async def _job():
        raise RuntimeError("boom")

    task = spawn(_job(), name="boomer")
    try:
        await task
    except RuntimeError:
        pass
    # Done-callbacks run on the next loop iteration.
    await asyncio.sleep(0)

    messages = [rec.getMessage() for rec in captured_util_tasks_logs]
    assert any("boomer" in m for m in messages), messages
    assert pending_count() == 0


async def test_spawn_silent_on_cancellation(captured_util_tasks_logs):
    async def _job():
        await asyncio.sleep(60)

    task = spawn(_job(), name="sleeper")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)

    # Cancellation must not trip the error-logging done-callback.
    assert captured_util_tasks_logs == []
