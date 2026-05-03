"""Tests for util.tasks.spawn — error logging and registry behaviour."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from util.tasks import pending_count, spawn  # noqa: E402


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


async def test_spawn_logs_uncaught_exception(caplog):
    async def _job():
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="util.tasks"):
        task = spawn(_job(), name="boomer")
        # Wait for the task itself, then drain done-callbacks one tick later.
        try:
            await task
        except RuntimeError:
            pass
        await asyncio.sleep(0)

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("boomer" in m for m in messages), messages
    assert pending_count() == 0


async def test_spawn_silent_on_cancellation(caplog):
    async def _job():
        await asyncio.sleep(60)

    with caplog.at_level(logging.ERROR, logger="util.tasks"):
        task = spawn(_job(), name="sleeper")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    # Cancellation must not trip the error-logging done-callback.
    error_records = [r for r in caplog.records if r.name == "util.tasks"]
    assert error_records == []
