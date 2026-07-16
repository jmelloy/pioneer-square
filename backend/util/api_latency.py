"""Structured latency logging for outbound API calls (GitHub, LLM providers, etc.).

Every call site wraps its request in ``track_api_call`` and fills in the
``ApiCallMetrics`` it yields. On exit exactly one ``key=value``-formatted line
is emitted through the standard ``logging`` module (under the ``api.latency``
logger), so it inherits whatever handler/formatter the process already has
configured (colored, plain, or JSON — see logging_config.py) while staying
queryable by field regardless of format.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger("api.latency")


class ApiCallMetrics:
    """Mutable bag of fields filled in by the caller inside a ``track_api_call`` block."""

    __slots__ = ("status_code", "input_tokens", "output_tokens", "error")

    def __init__(self) -> None:
        self.status_code: int | str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.error: str | None = None


@contextmanager
def track_api_call(provider: str, endpoint: str, method: str = "GET") -> Iterator[ApiCallMetrics]:
    """Time one outbound API call and log provider/endpoint/method/status/elapsed_ms.

    Usage::

        with track_api_call("github", "/repos/x/y/issues", method="GET") as call:
            resp = await client.get(...)
            call.status_code = resp.status_code

    An exception raised inside the block is recorded on ``metrics.error``,
    logged at WARNING, and re-raised unchanged — callers don't need their own
    try/except just to get a latency log line out of a failed call.
    """
    metrics = ApiCallMetrics()
    start = time.monotonic()
    try:
        yield metrics
    except Exception as exc:
        metrics.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        fields = [
            f"provider={provider}",
            f"endpoint={endpoint}",
            f"method={method}",
            f"status={metrics.status_code if metrics.status_code is not None else 'error'}",
            f"elapsed_ms={elapsed_ms:.1f}",
        ]
        if metrics.input_tokens is not None:
            fields.append(f"input_tokens={metrics.input_tokens}")
        if metrics.output_tokens is not None:
            fields.append(f"output_tokens={metrics.output_tokens}")
        if metrics.error is not None:
            fields.append(f"error={metrics.error!r}")
        level = logging.WARNING if metrics.error is not None else logging.INFO
        logger.log(level, "api_call " + " ".join(fields))
