"""Tests for the tiered-auth MCP endpoint (``POST /mcp``)."""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from helpers import insert_guild, insert_task, make_auth_token  # noqa: E402
from routes import mcp as mcp_routes  # noqa: E402

_DEBUG = "mcp-debug-token"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", _DEBUG)
    mcp_routes._debug_calls.clear()


def _rpc(c, method, params=None, *, token=None, debug=None, guild=None, rid=1):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if debug:
        headers["X-Debug-Token"] = debug
    if guild:
        headers["X-Guild-Id"] = guild
    return c.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
    )


def _setup(client):
    c, db_url = client
    gid = "m" + uuid.uuid4().hex[:5]
    insert_guild(db_url, gid)
    insert_task(db_url, gid, "t-" + gid, description="hello")
    return c, gid, make_auth_token(db_url), "t-" + gid


def test_initialize_and_list_hide_debug_tools(client):
    c, *_ = _setup(client)
    assert _rpc(c, "initialize").json()["result"]["serverInfo"]["name"] == "pioneer-square"
    names = [t["name"] for t in _rpc(c, "tools/list").json()["result"]["tools"]]
    assert names == ["list_tasks", "get_task"]
    names = [t["name"] for t in _rpc(c, "tools/list", debug=_DEBUG).json()["result"]["tools"]]
    assert "debug_query" in names and "cloudflare_logs" in names
    names = [t["name"] for t in _rpc(c, "tools/list", debug="wrong").json()["result"]["tools"]]
    assert "debug_query" not in names


def test_normal_tool_requires_auth(client):
    c, gid, token, tid = _setup(client)
    call = {"name": "list_tasks", "arguments": {"guild_id": gid}}
    assert _rpc(c, "tools/call", call).status_code == 401
    assert _rpc(c, "tools/call", call, token="bogus").status_code == 401
    r = _rpc(c, "tools/call", call, token=token)
    assert r.status_code == 200 and tid in r.json()["result"]["content"][0]["text"]


def test_get_task_not_found_is_tool_error(client):
    c, gid, token, _ = _setup(client)
    r = _rpc(
        c,
        "tools/call",
        {"name": "get_task", "arguments": {"guild_id": gid, "task_id": "x"}},
        token=token,
    )
    assert r.json()["result"]["isError"] is True


def test_normal_token_cannot_call_debug_tool(client):
    c, gid, token, _ = _setup(client)
    call = {"name": "debug_query", "arguments": {"sql": "SELECT id FROM tasks"}}
    assert _rpc(c, "tools/call", call, token=token, guild=gid).status_code == 403
    assert _rpc(c, "tools/call", call, token=token, guild=gid, debug="wrong").status_code == 403


def test_debug_token_alone_is_insufficient(client):
    c, gid, _, _ = _setup(client)
    call = {"name": "debug_query", "arguments": {"sql": "SELECT id FROM tasks"}}
    assert _rpc(c, "tools/call", call, debug=_DEBUG, guild=gid).status_code == 401


def test_debug_disabled_when_env_unset(client, monkeypatch):
    c, gid, token, _ = _setup(client)
    monkeypatch.delenv("DEBUG_TOKEN")
    call = {"name": "debug_query", "arguments": {"sql": "SELECT id FROM tasks"}}
    assert _rpc(c, "tools/call", call, token=token, guild=gid, debug="").status_code == 403


def test_debug_query_works_and_is_read_only(client, caplog):
    c, gid, token, tid = _setup(client)
    caplog.set_level("INFO", logger="mcp.audit")
    ok = _rpc(
        c,
        "tools/call",
        {"name": "debug_query", "arguments": {"sql": f"SELECT id FROM tasks WHERE id = '{tid}'"}},
        token=token,
        debug=_DEBUG,
        guild=gid,
    )
    assert tid in ok.json()["result"]["content"][0]["text"]
    assert "debug call" in caplog.text
    bad = _rpc(
        c,
        "tools/call",
        {"name": "debug_query", "arguments": {"sql": "SELECT * FROM user_sessions"}},
        token=token,
        debug=_DEBUG,
        guild=gid,
    )
    assert bad.json()["result"]["isError"] is True
    dml = _rpc(
        c,
        "tools/call",
        {"name": "debug_query", "arguments": {"sql": "DELETE FROM tasks"}},
        token=token,
        debug=_DEBUG,
        guild=gid,
    )
    assert dml.json()["result"]["isError"] is True


def test_debug_rate_limit(client, monkeypatch):
    c, gid, token, _ = _setup(client)
    monkeypatch.setattr(mcp_routes, "DEBUG_RATE_LIMIT", 2)
    call = {"name": "cloudflare_logs", "arguments": {}}
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    codes = [
        _rpc(c, "tools/call", call, token=token, debug=_DEBUG, guild=gid).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


def test_cloudflare_logs_calls_api(client, monkeypatch):
    c, gid, token, _ = _setup(client)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    seen = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"result": {"events": [1]}}

    class _Client:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers, json):
            seen.update(url=url, headers=headers, body=json)
            return _Resp()

    monkeypatch.setattr(mcp_routes.httpx, "AsyncClient", _Client)
    r = _rpc(
        c,
        "tools/call",
        {"name": "cloudflare_logs", "arguments": {"script": "w"}},
        token=token,
        debug=_DEBUG,
        guild=gid,
    )
    assert '"events"' in r.json()["result"]["content"][0]["text"]
    assert "acct" in seen["url"] and seen["headers"]["Authorization"] == "Bearer cf"


def test_protocol_errors(client):
    c, *_ = _setup(client)
    assert c.post("/mcp", content="nope").status_code == 400
    assert _rpc(c, "bogus").json()["error"]["code"] == -32601


@pytest.mark.parametrize(
    "params",
    [
        [1, 2],
        "str",
        5,
        {"name": ["x"], "arguments": {}},
        {"name": "list_tasks", "arguments": [1]},
        {"name": "list_tasks", "arguments": "x"},
        {"name": 7},
    ],
)
def test_malformed_params_return_invalid_params(client, params):
    c, gid, token, _ = _setup(client)
    r = _rpc(c, "tools/call", params, token=token)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32602


def test_non_numeric_limit_is_invalid_params(client):
    c, gid, token, _ = _setup(client)
    call = {"name": "list_tasks", "arguments": {"guild_id": gid, "limit": "abc"}}
    r = _rpc(c, "tools/call", call, token=token)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32602
