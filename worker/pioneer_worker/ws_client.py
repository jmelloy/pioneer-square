"""Resilient WebSocket client wrapper used by the worker.

Provides ``connect()``, ``send()``, ``recv()`` with automatic reconnection
on transport errors. Higher-level message dispatch is the caller's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
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


def _backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter."""
    exp = min(cap, base * (2 ** attempt))
    return random.uniform(0, exp)


class WSClient:
    def __init__(
        self,
        url: str,
        *,
        max_backoff: float = 30.0,
        base_backoff: float = 1.0,
        send_retries: int = 3,
    ) -> None:
        self.url = url
        self.max_backoff = max_backoff
        self.base_backoff = base_backoff
        self.send_retries = max(1, send_retries)
        self._ws = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """Connect (or reconnect) with exponential backoff and jitter.

        Retries indefinitely — the worker is a long-running daemon, so a transient
        backend outage shouldn't kill it. Backoff caps at ``max_backoff``.
        """
        async with self._lock:
            if self._ws is not None and _is_open(self._ws):
                return self._ws
            attempt = 0
            while True:
                try:
                    logger.info("Connecting to %s (attempt %d)", self.url, attempt + 1)
                    self._ws = await websockets.connect(
                        self.url, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024
                    )
                    if attempt > 0:
                        logger.info("WebSocket reconnected after %d attempts", attempt + 1)
                    else:
                        logger.info("WebSocket connected")
                    return self._ws
                except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
                    delay = _backoff_delay(attempt, self.base_backoff, self.max_backoff)
                    logger.warning(
                        "WS connect failed (attempt %d): %s — retrying in %.1fs",
                        attempt + 1, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1

    async def send(self, payload: dict) -> None:
        """Send a JSON payload, reconnecting on failure.

        Retries up to ``send_retries`` times with exponential backoff before
        giving up and raising. Transport-level reconnection is handled by
        ``connect()``; this loop guards against the socket flapping.
        """
        try:
            data = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            logger.error("WS send: payload is not JSON-serializable: %s", exc)
            raise

        last_exc: Optional[BaseException] = None
        for attempt in range(self.send_retries):
            try:
                ws = await self.connect()
                await ws.send(data)
                return
            except (websockets.WebSocketException, ConnectionError, OSError) as exc:
                last_exc = exc
                logger.warning(
                    "WS send failed (attempt %d/%d, type=%s): %s",
                    attempt + 1, self.send_retries, payload.get("type"), exc,
                )
                await self.close()
                if attempt + 1 < self.send_retries:
                    delay = _backoff_delay(attempt, self.base_backoff, self.max_backoff)
                    await asyncio.sleep(delay)
        logger.error(
            "WS send giving up after %d attempts (type=%s): %s",
            self.send_retries, payload.get("type"), last_exc,
        )
        if last_exc is not None:
            raise last_exc

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
                logger.warning(
                    "WS closed (code=%s reason=%s); reconnecting",
                    getattr(exc, "code", "?"), getattr(exc, "reason", "") or "n/a",
                )
                await self.close()
            except (OSError, asyncio.TimeoutError) as exc:
                logger.warning("WS transport error: %s; reconnecting", exc)
                await self.close()
                await asyncio.sleep(_backoff_delay(0, self.base_backoff, self.max_backoff))
            except Exception as exc:  # pragma: no cover
                logger.exception("WS recv error: %s", exc)
                await self.close()
                await asyncio.sleep(_backoff_delay(0, self.base_backoff, self.max_backoff))

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:
                logger.debug("WS close raised (ignored): %s", exc)
            self._ws = None
