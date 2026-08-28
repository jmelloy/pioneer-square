"""Tests for the mock worker (``pioneer-worker --mock``).

Two layers of coverage:

  Unit: the script parser, the script runner, and the HTTP-driven outcome
        path of ``_execute_task`` exercised by directly resolving the future
        the agent loop is waiting on. The WebSocket and registration steps
        are stubbed out so these stay fast and offline.

  Integration: a fully-wired ``MockWorker`` instance is constructed with its
        ``_send`` replaced by a sink, the HTTP server is started against a
        real port (bound to 127.0.0.1), and end-to-end requests drive state
        transitions. ``run()`` is not invoked; we exercise the same callable
        surface the HTTP handler thread uses.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from pioneer_worker.config import Config
from pioneer_worker.mock_worker import MockWorker, _extract_script
from pioneer_worker.runner_types import StopReason  # pyright: ignore[reportMissingImports]


def _make_cfg() -> Config:
    return Config(
        backend_url="ws://localhost:8000",
        guild_id="g-test",
        worker_id="w-mock01",
        max_agents=2,
        pull_interval=3600.0,
        repos=["mock/mock"],
    )


def _make_worker() -> MockWorker:
    worker = MockWorker(_make_cfg(), mock_api_port=0)
    worker._send = AsyncMock()
    worker._worker_name = "MockWorker-1"
    return worker


def _sent_types(worker: MockWorker) -> list[str]:
    return [c.args[0]["type"] for c in worker._send.await_args_list]


def _last_send_of(worker: MockWorker, mtype: str) -> dict | None:
    for call in reversed(worker._send.await_args_list):
        if call.args[0]["type"] == mtype:
            return call.args[0]
    return None


# ── Script extraction ──────────────────────────────────────────────────────


def test_extract_script_parses_inline_json():
    task = {
        "description": 'Do the thing\nMOCK_SCRIPT: [{"state": "working"}, {"complete": {}}]\nmore'
    }
    script = _extract_script(task)
    assert script == [{"state": "working"}, {"complete": {}}]


def test_extract_script_returns_none_when_absent():
    assert _extract_script({"description": "nothing special here"}) is None
    assert _extract_script({}) is None


def test_extract_script_returns_none_on_bad_json():
    assert _extract_script({"description": "MOCK_SCRIPT: not-json"}) is None


def test_extract_script_rejects_non_array():
    assert _extract_script({"description": 'MOCK_SCRIPT: {"state": "working"}'}) is None


# ── Setup methods are no-ops ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_setup_methods_are_noops():
    worker = _make_worker()
    # All of these would normally make network/subprocess calls — verify they
    # return without doing anything observable.
    await worker._fetch_github_token_if_needed()
    await worker._refresh_github_repos()
    await worker._check_gh_auth()
    await worker._detect_available_tools()
    await worker._initial_worktree_sweep()
    assert worker._send.await_count == 0


# ── _execute_task: HTTP-driven completion ──────────────────────────────────


@pytest.mark.asyncio
async def test_execute_task_waits_for_http_complete():
    """No MOCK_SCRIPT → task parks on the future until /tasks/{id}/complete."""
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    slot = worker.agents[0]
    task = {"id": "t-abc123", "description": "Fix the thing", "name": "Fix"}

    async def _drive() -> None:
        # Wait for _execute_task to install its future, then resolve it.
        for _ in range(50):
            if "t-abc123" in worker._task_outcomes:
                break
            await asyncio.sleep(0.005)
        worker._resolve_task_outcome(
            "t-abc123",
            {"kind": "complete", "prUrl": "https://example.com/pr/1", "lastText": "done!"},
        )

    driver = asyncio.create_task(_drive())
    await worker._execute_task(task, slot)
    await driver

    types = _sent_types(worker)
    assert "task-complete" in types, types
    complete = _last_send_of(worker, "task-complete")
    assert complete["taskId"] == "t-abc123"
    assert complete["prUrl"] == "https://example.com/pr/1"
    assert complete["lastText"] == "done!"
    # Final state must be idle on the slot.
    idle = _last_send_of(worker, "agent-state")
    assert idle["state"] == "idle"
    # Slot is no longer in active tasks.
    assert "t-abc123" not in worker._task_outcomes


@pytest.mark.asyncio
async def test_execute_task_followup_sends_followup_done():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    slot = worker.agents[0]
    task = {
        "id": "t-fu1",
        "description": "Follow up",
        "followup_instructions": "Tweak it",
        "followup_branch": "mock/fu-branch",
    }

    async def _drive() -> None:
        for _ in range(50):
            if "t-fu1" in worker._task_outcomes:
                break
            await asyncio.sleep(0.005)
        worker._resolve_task_outcome("t-fu1", {"kind": "complete"})

    driver = asyncio.create_task(_drive())
    await worker._execute_task(task, slot)
    await driver

    assert "task-followup-done" in _sent_types(worker)
    assert "task-complete" not in _sent_types(worker)
    done = _last_send_of(worker, "task-followup-done")
    assert done["branch"] == "mock/fu-branch"
    assert done["success"] is True


async def _drive_fail(worker: MockWorker, task_id: str, stop_reason: str) -> None:
    """Resolve *task_id*'s pending HTTP outcome as a failure with *stop_reason*."""
    for _ in range(50):
        if task_id in worker._task_outcomes:
            break
        await asyncio.sleep(0.005)
    worker._resolve_task_outcome(
        task_id,
        {"kind": "fail", "stopReason": stop_reason, "lastMessage": "stuck"},
    )


@pytest.mark.asyncio
async def test_execute_task_http_fail_emits_error_not_needs_input():
    """An ordinary failure is a plain error task-update, not a human escalation.

    The mock used to send type="needs-input" on *every* failure, which made the
    backend's handle_needs_input look exercised while the branch production
    actually takes went untested (#1238).
    """
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    slot = worker.agents[0]
    task = {"id": "t-fail1", "description": "Will fail"}

    driver = asyncio.create_task(_drive_fail(worker, "t-fail1", "max_turns"))
    await worker._execute_task(task, slot)
    await driver

    assert _last_send_of(worker, "needs-input") is None
    update = _last_send_of(worker, "task-update")
    assert update is not None
    assert update["state"] == "error"
    assert update["stopReason"] == "max_turns"
    assert update["lastMessage"] == "stuck"

    # Slot went through error before settling on idle.
    states_seen = [
        c.args[0]["state"]
        for c in worker._send.await_args_list
        if c.args[0].get("type") == "agent-state"
    ]
    assert "error" in states_seen
    assert states_seen[-1] == "idle"


@pytest.mark.asyncio
async def test_execute_task_needs_input_escalates_to_human():
    """Only a NEEDS_INPUT stop reason reaches the backend's escalation handler."""
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    slot = worker.agents[0]
    task = {"id": "t-fail2", "description": "Will ask"}

    driver = asyncio.create_task(_drive_fail(worker, "t-fail2", StopReason.NEEDS_INPUT))
    await worker._execute_task(task, slot)
    await driver

    needs = _last_send_of(worker, "needs-input")
    assert needs is not None
    assert needs["stopReason"] == StopReason.NEEDS_INPUT
    assert needs["lastMessage"] == "stuck"


# ── Scripted execution ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scripted_task_runs_to_completion_without_http():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    slot = worker.agents[0]
    script = [
        {"state": "thinking", "delay_ms": 0},
        {"output": "[mock] reading source"},
        {"state": "working", "activity": "editing"},
        {"output": "[mock] applying patch"},
        {"complete": {"prUrl": "https://example.com/pr/42", "lastText": "all green"}},
    ]
    task = {
        "id": "t-script1",
        "description": f"Headline\nMOCK_SCRIPT: {json.dumps(script)}\n",
        "name": "scripted",
    }

    await worker._execute_task(task, slot)

    states = [
        c.args[0]["state"]
        for c in worker._send.await_args_list
        if c.args[0].get("type") == "agent-state"
    ]
    # Final transition must end at idle; we should have seen thinking and working.
    assert "thinking" in states
    assert "working" in states
    assert states[-1] == "idle"

    complete = _last_send_of(worker, "task-complete")
    assert complete is not None
    assert complete["prUrl"] == "https://example.com/pr/42"
    assert complete["lastText"] == "all green"

    output_lines = [
        c.args[0]["line"]
        for c in worker._send.await_args_list
        if c.args[0].get("type") == "terminal-output"
    ]
    assert any("reading source" in line for line in output_lines)
    assert any("applying patch" in line for line in output_lines)


@pytest.mark.asyncio
async def test_scripted_task_can_fail():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    slot = worker.agents[0]
    script = [{"fail": {"stopReason": "boom", "lastMessage": "scripted failure"}}]
    task = {"id": "t-script-fail", "description": f"MOCK_SCRIPT: {json.dumps(script)}"}

    await worker._execute_task(task, slot)

    assert _last_send_of(worker, "needs-input") is None
    update = _last_send_of(worker, "task-update")
    assert update is not None
    assert update["state"] == "error"
    assert update["stopReason"] == "boom"


# ── HTTP server integration ────────────────────────────────────────────────


def _wait_until(predicate, timeout: float = 2.0, step: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


@pytest.mark.asyncio
async def test_http_api_set_state_emits_agent_state():
    """End-to-end: bind the HTTP server, POST set-state, verify _send fires."""
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    worker._start_http_server()
    try:
        port = worker.mock_api_port
        agent_id = worker.agents[0].id
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                f"/agents/{agent_id}/state",
                json={"state": "thinking", "activity": "reading", "taskId": "t-xyz"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["state"] == "thinking"
            assert body["activity"] == "reading"
            assert body["taskId"] == "t-xyz"

            status = (await client.get("/")).json()
            assert status["workerId"] == "w-mock01"
            slot_info = next(s for s in status["agents"] if s["agentId"] == agent_id)
            assert slot_info["state"] == "thinking"
            assert slot_info["activity"] == "reading"

        # The _send mock was called with the agent-state frame on the worker's loop.
        sent_state = _last_send_of(worker, "agent-state")
        assert sent_state is not None
        assert sent_state["workerId"] == "w-mock01"
        assert sent_state["agentId"] == agent_id
        assert sent_state["state"] == "thinking"
        assert sent_state["activity"] == "reading"
        assert sent_state["taskId"] == "t-xyz"
    finally:
        worker._stop_http_server()


@pytest.mark.asyncio
async def test_http_api_emit_output_includes_task_id():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    worker._start_http_server()
    try:
        port = worker.mock_api_port
        agent_id = worker.agents[1].id
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                f"/agents/{agent_id}/output",
                json={"line": "[mock] hi", "taskId": "t-out1"},
            )
            assert resp.status_code == 200, resp.text

        emitted = _last_send_of(worker, "terminal-output")
        assert emitted is not None
        assert emitted["line"] == "[mock] hi"
        assert emitted["taskId"] == "t-out1"
        assert emitted["agentId"] == agent_id
    finally:
        worker._stop_http_server()


@pytest.mark.asyncio
async def test_http_api_complete_resolves_waiting_task():
    """Spawn _execute_task in the background, then complete via HTTP."""
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    worker._start_http_server()
    try:
        port = worker.mock_api_port
        slot = worker.agents[0]
        task = {"id": "t-http-c1", "description": "Wait for HTTP"}
        exec_task = asyncio.create_task(worker._execute_task(task, slot))

        # Wait until the future is installed.
        for _ in range(50):
            if "t-http-c1" in worker._task_outcomes:
                break
            await asyncio.sleep(0.01)
        assert "t-http-c1" in worker._task_outcomes

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/tasks/t-http-c1/complete",
                json={"prUrl": "https://example.com/pr/9", "lastText": "ship it"},
            )
            assert resp.status_code == 200, resp.text

        await asyncio.wait_for(exec_task, timeout=2.0)
        complete = _last_send_of(worker, "task-complete")
        assert complete is not None
        assert complete["prUrl"] == "https://example.com/pr/9"
    finally:
        worker._stop_http_server()


@pytest.mark.asyncio
async def test_http_api_complete_unknown_task_returns_404():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    worker._start_http_server()
    try:
        port = worker.mock_api_port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post("/tasks/t-nope/complete", json={})
            assert resp.status_code == 404
            assert "error" in resp.json()
    finally:
        worker._stop_http_server()


@pytest.mark.asyncio
async def test_http_api_set_state_unknown_agent_returns_404():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    worker._start_http_server()
    try:
        port = worker.mock_api_port
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/agents/a-bogus/state",
                json={"state": "working"},
            )
            assert resp.status_code == 404
    finally:
        worker._stop_http_server()


@pytest.mark.asyncio
async def test_http_api_set_state_requires_state_field():
    worker = _make_worker()
    worker._loop = asyncio.get_running_loop()
    worker._start_http_server()
    try:
        port = worker.mock_api_port
        agent_id = worker.agents[0].id
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(f"/agents/{agent_id}/state", json={})
            assert resp.status_code == 400
    finally:
        worker._stop_http_server()
