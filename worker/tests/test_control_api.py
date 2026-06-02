"""Tests for the worker control API (``--api-port``).

Covers the task-injection and status methods on the base Worker directly, plus
an end-to-end pass over the threaded HTTP server bound to a real localhost
port. ``run()`` is never invoked; we wire ``_loop`` and ``_send`` the same way
the running worker would.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from pioneer_worker.config import Config
from pioneer_worker.control_api import ControlServer
from pioneer_worker.worker import Worker


def _make_cfg(**kw) -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="g-test",
        worker_id="w-ctrl01",
        max_agents=2,
        pull_interval=3600.0,
        repos=["owner/repo"],
        **kw,
    )


def _make_worker() -> Worker:
    worker = Worker(_make_cfg())
    worker._send = AsyncMock()
    worker._worker_name = "ControlWorker-1"
    return worker


# ── Task injection (unit) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_task_enqueues_and_tracks():
    worker = _make_worker()
    result = await worker._inject_task({"description": "Do a thing", "name": "thing"})
    assert result["ok"] is True
    task_id = result["taskId"]
    assert task_id in worker._known_task_ids
    assert worker.task_queue.qsize() == 1
    queued = worker.task_queue.get_nowait()
    assert queued["description"] == "Do a thing"
    assert queued["name"] == "thing"
    assert queued["tool"] == "claude"
    assert queued["phase"] == "execute"


@pytest.mark.asyncio
async def test_inject_task_honors_explicit_id_and_fields():
    worker = _make_worker()
    result = await worker._inject_task(
        {
            "id": "t-custom",
            "description": "Codex task",
            "tool": "codex",
            "phase": "review",
            "repos": ["owner/repo"],
            "issueNumber": 7,
            "issueRepo": "owner/repo",
        }
    )
    assert result["taskId"] == "t-custom"
    queued = worker.task_queue.get_nowait()
    assert queued["tool"] == "codex"
    assert queued["phase"] == "review"
    assert queued["issue_number"] == 7
    assert queued["issue_repo"] == "owner/repo"
    assert queued["repos"] == ["owner/repo"]


@pytest.mark.asyncio
async def test_inject_task_followup_fields():
    worker = _make_worker()
    await worker._inject_task(
        {
            "id": "t-fu",
            "followupInstructions": "tweak it",
            "followupBranch": "claude/x-123",
        }
    )
    queued = worker.task_queue.get_nowait()
    assert queued["followup_instructions"] == "tweak it"
    assert queued["followup_branch"] == "claude/x-123"


@pytest.mark.asyncio
async def test_inject_task_requires_description():
    worker = _make_worker()
    result = await worker._inject_task({"name": "no body"})
    assert "error" in result
    assert worker.task_queue.qsize() == 0


@pytest.mark.asyncio
async def test_inject_task_rejects_duplicate_id():
    worker = _make_worker()
    await worker._inject_task({"id": "t-dup", "description": "first"})
    result = await worker._inject_task({"id": "t-dup", "description": "second"})
    assert "error" in result
    assert worker.task_queue.qsize() == 1


def test_status_snapshot_shape():
    worker = _make_worker()
    snap = worker._status_snapshot()
    assert snap["workerId"] == "w-ctrl01"
    assert snap["guildId"] == "g-test"
    assert len(snap["agents"]) == 2
    assert snap["queueDepth"] == 0
    assert snap["knownTaskIds"] == []


# ── HTTP server integration ────────────────────────────────────────────────


def _wait_until(predicate, timeout: float = 2.0, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


@pytest.mark.asyncio
async def test_http_status_and_inject():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    server = ControlServer(worker, host="127.0.0.1", port=0)
    server.start()
    try:
        port = server.port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            status = (await client.get("/")).json()
            assert status["workerId"] == "w-ctrl01"
            assert len(status["agents"]) == 2

            resp = await client.post("/tasks", json={"id": "t-http", "description": "via http"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ok"] is True
            assert body["taskId"] == "t-http"

            tasks = (await client.get("/tasks")).json()
            assert "t-http" in tasks["knownTaskIds"]
            assert tasks["queueDepth"] == 1

        assert worker.task_queue.get_nowait()["description"] == "via http"
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_http_inject_missing_description_returns_400():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    server = ControlServer(worker, host="127.0.0.1", port=0)
    server.start()
    try:
        port = server.port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/tasks", json={"name": "no body"})
            assert resp.status_code == 400
            assert "error" in resp.json()
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_http_shutdown_triggers_event():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    server = ControlServer(worker, host="127.0.0.1", port=0)
    server.start()
    try:
        port = server.port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/control/shutdown", json={})
            assert resp.status_code == 200
        assert _wait_until(worker._shutdown_event.is_set)
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_http_unknown_path_404():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    server = ControlServer(worker, host="127.0.0.1", port=0)
    server.start()
    try:
        port = server.port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            assert (await client.get("/nope")).status_code == 404
    finally:
        server.stop()
