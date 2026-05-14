"""Logging configuration with colors and JSON support.

Usage (backend startup):
    import logging.config
    from logging_config import get_logging_config
    logging.config.dictConfig(get_logging_config(log_level="INFO"))

The config covers:
- Colored or JSON output via LOG_FORMAT env var
- Root logger at the requested level (catches all app namespaces)
- uvicorn.error / uvicorn.access wired to their own handlers so they
  don't double-log through the root handler
- foreman forced to DEBUG so A2A client request/response lines are
  visible when LOG_LEVEL=DEBUG
- Noisy third-party loggers (aiosqlite, httpx, websockets, …) capped
  at WARNING to avoid drowning out application logs
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

COLORS = {
    "DEBUG": "\033[36m",  # Cyan
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[35m",  # Magenta
}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


class ColoredFormatter(logging.Formatter):
    """Formatter that adds ANSI color codes for terminal display."""

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        use_colors: bool = True,
    ) -> None:
        super().__init__(fmt, datefmt)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        original_name = record.name

        if self.use_colors:
            color = COLORS.get(record.levelname, "")
            record.levelname = f"{color}{BOLD}{record.levelname:8}{RESET}"
            record.name = f"{DIM}{record.name}{RESET}"

        result = super().format(record)

        record.levelname = original_levelname
        record.name = original_name
        return result


class JSONFormatter(logging.Formatter):
    """Formatter that outputs log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.pathname:
            log_data["location"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, default=str)


# Third-party loggers that produce excessive DEBUG/INFO chatter.  We cap them
# at WARNING so application logs remain readable at LOG_LEVEL=DEBUG.
_SUPPRESSED_LOGGERS = [
    "aiosqlite",
    "asyncio",
    "httpcore",
    "httpx",
    "websockets",
    "urllib3",
]


def get_logging_config(
    log_level: str = "INFO",
    log_format: str = "colored",
) -> dict[str, Any]:
    """Return a dict suitable for ``logging.config.dictConfig()``.

    Args:
        log_level:  Root log level string (DEBUG, INFO, WARNING, …).
        log_format: ``"colored"`` (default), ``"json"``, or ``"plain"``.
    """
    use_json = log_format == "json"
    use_colors = log_format == "colored"

    if use_json:
        default_formatter: dict[str, Any] = {"()": JSONFormatter}
        access_formatter: dict[str, Any] = {"()": JSONFormatter}
    else:
        default_formatter = {
            "()": ColoredFormatter,
            "fmt": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
            "use_colors": use_colors,
        }
        access_formatter = {
            "()": ColoredFormatter,
            "fmt": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
            "use_colors": use_colors,
        }

    suppressed_loggers: dict[str, Any] = {
        name: {"level": "WARNING", "propagate": True} for name in _SUPPRESSED_LOGGERS
    }

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": default_formatter,
            "access": access_formatter,
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        # Root logger catches all application namespaces (main, ws_handlers,
        # routes.*, foreman.*, …) that use getLogger(__name__).
        "root": {
            "handlers": ["default"],
            "level": log_level,
        },
        "loggers": {
            # uvicorn manages its own error/access loggers; give them dedicated
            # handlers and disable propagation to avoid double-logging.
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
            # foreman is always at DEBUG so A2A client request/response lines
            # (logger.info / logger.debug in foreman/a2a_client.py) are visible
            # when LOG_LEVEL=DEBUG without forcing the entire root to DEBUG.
            "foreman": {
                "level": "DEBUG",
                "propagate": True,
            },
            **suppressed_loggers,
        },
    }
