"""External Foreman API proxy request/response coordination.

The backend still owns Foreman state, tool execution, history, polling, and
locking. When a standalone proxy is connected, the embedded runner uses this
module only to ask that process to execute an LLM API request that the backend
cannot or should not make directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from events import foreman_connections, send_ws_message
from fastapi import WebSocket
from ws_types import ForemanApiRequestMsg

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECS = float(os.environ.get("FOREMAN_PROXY_API_TIMEOUT", "600"))


class ForemanProxyUnavailable(RuntimeError):
    """Raised when no external API proxy is connected for a guild."""


class ForemanProxyError(RuntimeError):
    """Raised when the external API proxy returns an error response."""


@dataclass
class _PendingRequest:
    guild_id: str
    websocket: WebSocket
    future: asyncio.Future[dict[str, Any]]


_pending_requests: dict[str, _PendingRequest] = {}


def has_foreman_proxy(guild_id: str) -> bool:
    """Return True when an external API proxy currently owns this guild."""
    return foreman_connections.get(guild_id) is not None


async def call_foreman_api_proxy(
    guild_id: str,
    *,
    model: str,
    max_tokens: int,
    system: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: dict[str, Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Send one LLM request to the connected proxy and await its response."""
    ws = foreman_connections.get(guild_id)
    if ws is None:
        raise ForemanProxyUnavailable(f"no foreman API proxy connected for guild {guild_id}")

    request_id = f"fapi-{uuid4().hex}"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_requests[request_id] = _PendingRequest(guild_id, ws, future)

    try:
        await send_ws_message(
            ws,
            ForemanApiRequestMsg(
                requestId=request_id,
                guildId=guild_id,
                model=model,
                maxTokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
                toolChoice=tool_choice,
            ),
        )
        data = await asyncio.wait_for(future, timeout=timeout)
    except Exception:
        _pending_requests.pop(request_id, None)
        raise
    finally:
        _pending_requests.pop(request_id, None)

    if not data.get("ok", True):
        raise ForemanProxyError(data.get("error") or "foreman API proxy returned an error")
    response = data.get("response")
    if not isinstance(response, dict):
        raise ForemanProxyError("foreman API proxy returned no response payload")
    return data


def resolve_foreman_api_response(data: dict[str, Any]) -> bool:
    """Resolve a pending API proxy request from an inbound WS response frame."""
    request_id = data.get("requestId")
    if not request_id:
        return False
    pending = _pending_requests.get(request_id)
    if pending is None:
        logger.warning("Ignoring foreman-api-response for unknown requestId=%s", request_id)
        return False
    if data.get("guildId") not in (None, pending.guild_id):
        logger.warning(
            "Ignoring foreman-api-response for requestId=%s with guildId=%s; expected %s",
            request_id,
            data.get("guildId"),
            pending.guild_id,
        )
        return False
    if not pending.future.done():
        pending.future.set_result(data)
    return True


def fail_pending_for_websocket(websocket: WebSocket, reason: str) -> None:
    """Fail all outstanding API requests owned by a disconnecting proxy socket."""
    for request_id, pending in list(_pending_requests.items()):
        if pending.websocket is not websocket:
            continue
        _pending_requests.pop(request_id, None)
        if not pending.future.done():
            pending.future.set_exception(ForemanProxyUnavailable(reason))
