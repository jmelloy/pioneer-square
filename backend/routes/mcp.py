"""MCP (Model Context Protocol) endpoint with two authentication tiers.

``POST /mcp`` speaks stateless JSON-RPC 2.0 over HTTP (the MCP "streamable HTTP"
transport, JSON-response mode). It is implemented directly rather than via the
``mcp`` SDK to avoid a new dependency; the subset needed by tool-only servers
(``initialize``, ``ping``, ``tools/list``, ``tools/call``) is small.

Tiers:
  * **Normal** — ``Authorization: Bearer <token>`` (member login token, worker
    token or foreman JWT, validated per guild by ``authorize_worker_or_member``).
    Tools take a ``guild_id`` argument and are authorized against it.
  * **Debug** — additionally requires ``X-Debug-Token`` matching the
    ``DEBUG_TOKEN`` env var *and* a valid normal Bearer token (so every debug
    call is attributable to a principal). Debug tools are neither listed nor
    callable without it; they are not guild-scoped.

Debug access is audit-logged (logger ``mcp.audit``) and rate-limited per
principal (in-memory, per process).
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from typing import Any

import httpx
from auth_deps import authorize_worker_or_member, check_debug_token, get_guild_pk, http_bearer
from database import get_db
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from models import Task
from routes.debug_query import execute_readonly_query
from sqlmodel import col, select

router = APIRouter(tags=["mcp"])
audit_log = logging.getLogger("mcp.audit")

PROTOCOL_VERSION = "2025-03-26"
DEBUG_RATE_LIMIT = 30  # debug tool calls ...
DEBUG_RATE_WINDOW_S = 60  # ... per window, per principal
_debug_calls: dict[str, deque[float]] = defaultdict(deque)

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

_JSON_ERR_PARSE, _JSON_ERR_REQUEST, _JSON_ERR_METHOD, _JSON_ERR_PARAMS = (
    -32700,
    -32600,
    -32601,
    -32602,
)
_JSON_ERR_INTERNAL = -32603

NORMAL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_tasks",
        "description": "List recent tasks in a guild.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "guild_id": {"type": "string"},
                "state": {"type": "string", "description": "Optional state filter"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": ["guild_id"],
        },
    },
    {
        "name": "get_task",
        "description": "Get one task by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"guild_id": {"type": "string"}, "task_id": {"type": "string"}},
            "required": ["guild_id", "task_id"],
        },
    },
]

DEBUG_TOOLS: list[dict[str, Any]] = [
    {
        "name": "debug_query",
        "description": "Run a read-only SELECT against allow-listed operational tables.",
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "cloudflare_logs",
        "description": (
            "Query Cloudflare Workers logs (requires CLOUDFLARE_API_TOKEN and "
            "CLOUDFLARE_ACCOUNT_ID)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "Worker script name filter"},
                "search": {"type": "string", "description": "Free-text filter"},
                "minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 15},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
    },
]


class ToolError(Exception):
    """Reported to the client as an MCP tool result with ``isError``."""


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _task_dict(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "state": t.state,
        "phase": t.phase,
        "branch": t.branch,
        "pr_url": t.pr_url,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


async def _list_tasks(args: dict, _principal: str) -> Any:
    limit = min(int(args.get("limit", 20)), 100)
    db = await get_db()
    try:
        pk = await get_guild_pk(db, args["guild_id"])
        q = select(Task).where(col(Task.guild_id) == pk, col(Task.deleted_at).is_(None))
        if args.get("state"):
            q = q.where(col(Task.state) == args["state"])
        rows = (await db.exec(q.order_by(col(Task.created_at).desc()).limit(limit))).all()
        return [_task_dict(t) for t in rows]
    finally:
        await db.close()


async def _get_task(args: dict, _principal: str) -> Any:
    db = await get_db()
    try:
        pk = await get_guild_pk(db, args["guild_id"])
        t = (
            await db.exec(
                select(Task).where(
                    col(Task.guild_id) == pk,
                    col(Task.id) == args["task_id"],
                    col(Task.deleted_at).is_(None),
                )
            )
        ).one_or_none()
        if t is None:
            raise ToolError("Task not found")
        return _task_dict(t)
    finally:
        await db.close()


async def _debug_query(args: dict, _principal: str) -> Any:
    db = await get_db()
    try:
        rows = await execute_readonly_query(db, str(args.get("sql", "")))
        return rows
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc
    finally:
        await db.close()


async def _cloudflare_logs(args: dict, _principal: str) -> Any:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        raise ToolError("Cloudflare is not configured (CLOUDFLARE_API_TOKEN/ACCOUNT_ID)")
    now_ms = int(time.time() * 1000)
    minutes = min(int(args.get("minutes", 15)), 1440)
    filters: list[dict[str, Any]] = []
    if args.get("script"):
        filters.append(
            {
                "key": "$metadata.service",
                "operation": "eq",
                "type": "string",
                "value": args["script"],
            }
        )
    if args.get("search"):
        filters.append(
            {"key": "$metadata.message", "operation": "includes", "type": "string",
             "value": args["search"]}
        )  # fmt: skip
    body = {
        "queryId": "mcp-debug",
        "timeframe": {"from": now_ms - minutes * 60_000, "to": now_ms},
        "view": "events",
        "limit": min(int(args.get("limit", 50)), 200),
        "parameters": {"filters": filters, "calculations": []},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"{CLOUDFLARE_API}/accounts/{account}/workers/observability/telemetry/query",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise ToolError(f"Cloudflare request failed: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        raise ToolError(f"Cloudflare API returned {r.status_code}: {r.text[:500]}")
    return r.json().get("result", r.json())


_NORMAL_HANDLERS = {"list_tasks": _list_tasks, "get_task": _get_task}
_DEBUG_HANDLERS = {"debug_query": _debug_query, "cloudflare_logs": _cloudflare_logs}


# ---------------------------------------------------------------------------
# Auth + protocol
# ---------------------------------------------------------------------------


def _check_debug_rate_limit(principal: str) -> bool:
    now = time.monotonic()
    calls = _debug_calls[principal]
    while calls and now - calls[0] > DEBUG_RATE_WINDOW_S:
        calls.popleft()
    if len(calls) >= DEBUG_RATE_LIMIT:
        return False
    calls.append(now)
    return True


def _rpc_error(rid: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _tool_result(rid: Any, payload: Any, is_error: bool = False) -> JSONResponse:
    import json

    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
        }
    )


def _debug_enabled(x_debug_token: str | None) -> bool:
    """True when the request carries a valid debug token (never raises)."""
    if not x_debug_token or not os.environ.get("DEBUG_TOKEN"):
        return False
    try:
        check_debug_token(x_debug_token)
    except HTTPException:
        return False
    return True


@router.post("/mcp")
async def mcp_endpoint(  # noqa: C901
    request: Request,
    x_debug_token: str | None = Header(default=None),
    x_guild_id: str | None = Header(default=None),
):
    try:
        msg = await request.json()
    except ValueError:
        return _rpc_error(None, _JSON_ERR_PARSE, "Parse error", 400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _rpc_error(None, _JSON_ERR_REQUEST, "Invalid request", 400)
    rid, method = msg.get("id"), msg["method"]
    params = msg.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc_error(rid, _JSON_ERR_PARAMS, "params must be an object", 400)

    if "id" not in msg:  # notification (e.g. notifications/initialized)
        return Response(status_code=202)
    if method == "initialize":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pioneer-square", "version": "1"},
                },
            }
        )
    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {}})

    debug = _debug_enabled(x_debug_token)
    if method == "tools/list":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"tools": NORMAL_TOOLS + (DEBUG_TOOLS if debug else [])},
            }
        )
    if method != "tools/call":
        return _rpc_error(rid, _JSON_ERR_METHOD, f"Method not found: {method}")

    name, args = params.get("name"), params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(name, str) or not isinstance(args, dict):
        return _rpc_error(
            rid, _JSON_ERR_PARAMS, "name must be a string and arguments an object", 400
        )
    if name in _DEBUG_HANDLERS:
        # Missing/invalid debug credentials look identical to an unknown tool
        # only after the caller proves they are at least a normal user, so the
        # debug surface is not probeable anonymously.
        handler, tier = _DEBUG_HANDLERS[name], "debug"
    elif name in _NORMAL_HANDLERS:
        handler, tier = _NORMAL_HANDLERS[name], "normal"
    else:
        return _rpc_error(rid, _JSON_ERR_PARAMS, f"Unknown tool: {name}", 400)

    creds = await http_bearer(request)
    token = creds.credentials if creds else None
    guild_id = args.get("guild_id") or x_guild_id
    if tier == "normal":
        if not isinstance(guild_id, str) or not guild_id:
            return _rpc_error(rid, _JSON_ERR_PARAMS, "guild_id is required", 400)
        args["guild_id"] = guild_id
    elif not guild_id:
        return _rpc_error(
            rid, _JSON_ERR_PARAMS, "X-Guild-Id header is required for debug tools", 400
        )

    try:
        principal = await authorize_worker_or_member(guild_id, token)
    except HTTPException as exc:
        return _rpc_error(rid, -32001, str(exc.detail), exc.status_code)

    if tier == "debug":
        if not debug:
            audit_log.warning("debug access denied principal=%s tool=%s", principal, name)
            return _rpc_error(rid, -32002, "Valid X-Debug-Token required", 403)
        if not _check_debug_rate_limit(principal):
            audit_log.warning("debug rate limited principal=%s tool=%s", principal, name)
            return _rpc_error(rid, -32003, "Debug rate limit exceeded", 429)
        audit_log.info("debug call principal=%s tool=%s args=%r", principal, name, args)

    try:
        return _tool_result(rid, await handler(args, principal))
    except ToolError as exc:
        return _tool_result(rid, str(exc), is_error=True)
    except (KeyError, ValueError, TypeError) as exc:
        return _rpc_error(rid, _JSON_ERR_PARAMS, f"Invalid arguments: {exc}", 400)
    except Exception:
        logging.getLogger(__name__).exception("MCP tool %s failed", name)
        return _rpc_error(rid, _JSON_ERR_INTERNAL, "Internal error", 500)
