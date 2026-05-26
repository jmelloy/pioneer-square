"""Foreman tool definitions and HTTP-backed tool executor.

FOREMAN_TOOLS is imported from backend.foreman_core.tools_schema — the single source of
truth shared with the embedded foreman.  Tool execution is delegated to the
backend via POST /guilds/{guild_id}/foreman/exec_tool so all business logic
(DB writes, lock acquisition, WS broadcasts to workers) stays in the backend.
"""

from __future__ import annotations

import asyncio
import logging

from backend.foreman_core.tools_schema import (
    FOREMAN_TOOLS,  # noqa: F401  – re-exported for runner.py
)

from .http_client import ForemanHTTPClient

logger = logging.getLogger(__name__)


async def exec_tools(
    guild_id: str,
    tool_uses: list,
    *,
    http: ForemanHTTPClient,
    user_id: str | None = None,
) -> list:
    """Execute tool calls via the backend REST API.

    Independent tool calls run concurrently (same as the embedded executor).
    Results are returned in the same order as *tool_uses*.
    """
    coros = [
        http.exec_tool(
            tool_name=tu.name,
            tool_id=tu.id,
            tool_input=dict(tu.input) if tu.input else {},
            user_id=user_id,
        )
        for tu in tool_uses
    ]
    return list(await asyncio.gather(*coros))
