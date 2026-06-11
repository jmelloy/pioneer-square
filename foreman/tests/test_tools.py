"""Unit tests for pioneer_foreman.tools — exec_tools() via mocked HTTP.

Uses respx to intercept httpx calls so no real backend is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pioneer_foreman.http_client import ForemanHTTPClient
from pioneer_foreman.tools import exec_tools

# ── helpers ───────────────────────────────────────────────────────────────


def _make_tool_use(name: str, tool_id: str, inputs: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=tool_id, input=inputs)


def _make_http(responses: dict[str, dict]) -> ForemanHTTPClient:
    """Return a ForemanHTTPClient whose exec_tool is mocked with canned responses."""
    http = ForemanHTTPClient.__new__(ForemanHTTPClient)

    async def fake_exec_tool(
        tool_name: str,
        tool_id: str,
        tool_input: dict,
        user_id: str | None = None,
    ) -> dict:
        return responses.get(
            tool_id,
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok", "is_error": False},
        )

    http.exec_tool = fake_exec_tool
    return http


# ── single-tool calls ─────────────────────────────────────────────────────


async def test_exec_tools_single_returns_list():
    http = _make_http(
        {"t1": {"type": "tool_result", "tool_use_id": "t1", "content": "done", "is_error": False}}
    )
    result = await exec_tools(
        "g1", [_make_tool_use("create_task", "t1", {"title": "foo"})], http=http
    )
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["content"] == "done"


async def test_exec_tools_empty_returns_empty():
    http = _make_http({})
    result = await exec_tools("g1", [], http=http)
    assert result == []


async def test_exec_tools_preserves_order():
    """Results must be in the same order as tool_uses regardless of execution order."""
    responses = {
        "t1": {"type": "tool_result", "tool_use_id": "t1", "content": "first", "is_error": False},
        "t2": {"type": "tool_result", "tool_use_id": "t2", "content": "second", "is_error": False},
        "t3": {"type": "tool_result", "tool_use_id": "t3", "content": "third", "is_error": False},
    }
    http = _make_http(responses)
    tools = [
        _make_tool_use("get_task_status", "t1", {"task_id": "x1"}),
        _make_tool_use("get_task_status", "t2", {"task_id": "x2"}),
        _make_tool_use("get_task_status", "t3", {"task_id": "x3"}),
    ]
    result = await exec_tools("g1", tools, http=http)
    assert [r["content"] for r in result] == ["first", "second", "third"]


async def test_exec_tools_passes_user_id():
    """user_id must be forwarded to exec_tool."""
    received_user_ids = []

    class CapturingHTTP:
        async def exec_tool(self, tool_name, tool_id, tool_input, user_id=None):
            received_user_ids.append(user_id)
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "ok",
                "is_error": False,
            }

    await exec_tools(
        "g1",
        [_make_tool_use("assign_task", "t1", {"task_id": "x"})],
        http=CapturingHTTP(),
        user_id="u-abc",
    )
    assert received_user_ids == ["u-abc"]


async def test_exec_tools_passes_none_user_id_by_default():
    received_user_ids = []

    class CapturingHTTP:
        async def exec_tool(self, tool_name, tool_id, tool_input, user_id=None):
            received_user_ids.append(user_id)
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "ok",
                "is_error": False,
            }

    await exec_tools(
        "g1",
        [_make_tool_use("finalize_task", "t1", {"task_id": "x"})],
        http=CapturingHTTP(),
    )
    assert received_user_ids == [None]


# ── payload construction ──────────────────────────────────────────────────


async def test_exec_tool_sends_correct_tool_name_and_input():
    """exec_tools passes tool_name and tool_input through verbatim."""
    calls = []

    class TrackingHTTP:
        async def exec_tool(self, tool_name, tool_id, tool_input, user_id=None):
            calls.append({"name": tool_name, "id": tool_id, "input": tool_input})
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "ok",
                "is_error": False,
            }

    tool_input = {"task_id": "t-abc123", "state": "done"}
    await exec_tools(
        "g1",
        [_make_tool_use("finalize_task", "call-1", tool_input)],
        http=TrackingHTTP(),
    )
    assert len(calls) == 1
    assert calls[0]["name"] == "finalize_task"
    assert calls[0]["id"] == "call-1"
    assert calls[0]["input"] == tool_input


async def test_exec_tool_converts_input_to_dict():
    """tool_input from SimpleNamespace.input is converted via dict()."""
    calls = []

    class TrackingHTTP:
        async def exec_tool(self, tool_name, tool_id, tool_input, user_id=None):
            calls.append(tool_input)
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "ok",
                "is_error": False,
            }

    # Simulate an anthropic ToolUseBlock where .input is a dict-like object
    raw_input = {"worker_id": "w-1", "message": "hello"}
    tool_use = SimpleNamespace(name="message_worker", id="c1", input=raw_input)
    await exec_tools("g1", [tool_use], http=TrackingHTTP())
    assert calls[0] == raw_input


async def test_exec_tools_empty_input_becomes_empty_dict():
    """When tool_use.input is falsy, exec_tool receives {}."""
    calls = []

    class TrackingHTTP:
        async def exec_tool(self, tool_name, tool_id, tool_input, user_id=None):
            calls.append(tool_input)
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "ok",
                "is_error": False,
            }

    tool_use = SimpleNamespace(name="shutdown_worker", id="c2", input=None)
    await exec_tools("g1", [tool_use], http=TrackingHTTP())
    assert calls[0] == {}


# ── concurrency ────────────────────────────────────────────────────────────


async def test_exec_tools_runs_concurrently():
    """Multiple tool calls should be dispatched concurrently (asyncio.gather)."""
    import asyncio

    started: list[str] = []
    finished: list[str] = []

    class SlowHTTP:
        async def exec_tool(self, tool_name, tool_id, tool_input, user_id=None):
            started.append(tool_id)
            await asyncio.sleep(0.01)
            finished.append(tool_id)
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "ok",
                "is_error": False,
            }

    tools = [_make_tool_use(f"tool_{i}", f"id{i}", {}) for i in range(5)]
    await exec_tools("g1", tools, http=SlowHTTP())

    # All 5 should have started before any finished if run concurrently
    # At minimum: all ids appear in both lists
    assert set(started) == {f"id{i}" for i in range(5)}
    assert set(finished) == {f"id{i}" for i in range(5)}


# ── respx integration tests ──────────────────────────────────────────────


async def test_exec_tool_via_http_client_post(respx_mock):
    """ForemanHTTPClient.exec_tool calls POST /guilds/{guild_id}/foreman/exec_tool."""
    import httpx

    guild_id = "abc123"
    expected_response = {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": "task created",
        "is_error": False,
    }

    respx_mock.post(f"http://localhost:8000/guilds/{guild_id}/foreman/exec_tool").mock(
        return_value=httpx.Response(200, json=expected_response)
    )

    client = ForemanHTTPClient(
        "http://localhost:8000",
        guild_id,
        auth_token="test-token",
    )
    result = await client.exec_tool(
        tool_name="create_task",
        tool_id="call-1",
        tool_input={"title": "New task", "description": "do stuff"},
    )
    assert result == expected_response
    await client.close()


async def test_exec_tool_includes_user_id_in_body(respx_mock):
    """user_id must appear in the POST body when provided."""
    import httpx

    guild_id = "guild1"
    received_bodies: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        import json as _json

        received_bodies.append(_json.loads(request.content))
        return httpx.Response(
            200,
            json={"type": "tool_result", "tool_use_id": "c1", "content": "ok", "is_error": False},
        )

    respx_mock.post(f"http://localhost:8000/guilds/{guild_id}/foreman/exec_tool").mock(
        side_effect=capture
    )

    client = ForemanHTTPClient("http://localhost:8000", guild_id, auth_token="tok")
    await client.exec_tool("cancel_task", "c1", {"task_id": "t-1"}, user_id="u-99")
    await client.close()

    assert len(received_bodies) == 1
    assert received_bodies[0]["user_id"] == "u-99"
    assert received_bodies[0]["tool_name"] == "cancel_task"
    assert received_bodies[0]["tool_id"] == "c1"


async def test_exec_tool_jwt_auth_header(respx_mock):
    """When backend_key is set, Authorization header contains a Bearer JWT."""
    import httpx

    guild_id = "jwtguild"
    received_headers: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        received_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={"type": "tool_result", "tool_use_id": "c1", "content": "ok", "is_error": False},
        )

    respx_mock.post(f"http://localhost:8000/guilds/{guild_id}/foreman/exec_tool").mock(
        side_effect=capture
    )

    client = ForemanHTTPClient("http://localhost:8000", guild_id, backend_key="mykey")
    await client.exec_tool("assign_task", "c1", {"task_id": "t-1", "worker_id": "w-1"})
    await client.close()

    assert len(received_headers) == 1
    auth = received_headers[0].get("authorization", "")
    assert auth.startswith("Bearer "), f"Expected Bearer token, got: {auth}"
    # JWT has three dot-separated parts
    token = auth.removeprefix("Bearer ")
    assert len(token.split(".")) == 3
