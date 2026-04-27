"""Resilient WebSocket client wrapper used by the worker.

Provides ``connect()``, ``send()``, ``recv()`` with automatic reconnection
on transport errors. Higher-level message dispatch is the caller's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import websockets
from websockets.protocol import State

logger = logging.getLogger(__name__)


def _is_open(ws) -> bool:
    state = getattr(ws, "state", None)
    if state is not None:
        return state is State.OPEN
    # Older websockets versions exposed `.closed`.
    return not getattr(ws, "closed", True)


class WSClient:
    def __init__(self, url: str, *, max_backoff: float = 30.0) -> None:
        self.url = url
        self.max_backoff = max_backoff
        self._ws = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """Connect (or reconnect) with exponential backoff."""
        async with self._lock:
            if self._ws is not None and _is_open(self._ws):
                return self._ws
            backoff = 1.0
            while True:
                try:
                    logger.info("Connecting to %s", self.url)
                    self._ws = await websockets.connect(
                        self.url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024
                    )
                    logger.info("WebSocket connected")
                    return self._ws
                except (OSError, websockets.WebSocketException) as exc:
                    logger.warning("WS connect failed: %s — retrying in %.1fs", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(self.max_backoff, backoff * 2)

    async def send(self, payload: dict) -> None:
        ws = await self.connect()
        try:
            await ws.send(json.dumps(payload))
        except (websockets.WebSocketException, ConnectionError) as exc:
            logger.warning("WS send failed (%s); reconnecting", exc)
            await self.close()
            ws = await self.connect()
            await ws.send(json.dumps(payload))

    async def messages(self) -> AsyncIterator[dict]:
        """Yield JSON messages forever, reconnecting transparently."""
        while True:
            ws = await self.connect()
            try:
                async for raw in ws:
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Non-JSON WS frame ignored: %r", raw[:120])
            except websockets.ConnectionClosed as exc:
                logger.warning("WS closed (%s); reconnecting", exc)
                await self.close()
            except Exception as exc:  # pragma: no cover
                logger.exception("WS recv error: %s", exc)
                await self.close()
                await asyncio.sleep(1.0)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
