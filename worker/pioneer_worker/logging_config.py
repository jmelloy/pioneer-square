"""Logging configuration for the pioneer-worker process.

Mirrors the backend's logging_config.py: ColoredFormatter, JSONFormatter, and
a get_logging_config() factory that returns a dict for logging.config.dictConfig().

Usage (CLI entry point):
    import logging.config
    from pioneer_worker.logging_config import get_logging_config
    logging.config.dictConfig(get_logging_config(log_level="INFO"))
"""

from __future__ import annotations

import json
import logging
import os
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


# Third-party loggers that produce excessive DEBUG/INFO chatter.
_SUPPRESSED_LOGGERS = [
    "aiosqlite",
    "asyncio",
    "httpcore",
    "httpx",
    "websockets",
    "urllib3",
]


def running_on_ecs() -> bool:
    """Detect whether the process is running inside an ECS task.

    ECS (Fargate or EC2-backed) always sets a task metadata endpoint and,
    for Fargate, injects task-role credentials via a relative URI; both are
    reliable signals that we're in a container with no interactive
    terminal, so ANSI color codes would just show up as garbage in the log
    output (CloudWatch Logs et al.).
    """
    if "ECS_CONTAINER_METADATA_URI" in os.environ:
        return True
    if "ECS_CONTAINER_METADATA_URI_V4" in os.environ:
        return True
    if "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" in os.environ:
        return True
    if "ECS" in os.environ.get("AWS_EXECUTION_ENV", ""):
        return True
    return False


def default_log_format() -> str:
    """Return the log format to use when ``LOG_FORMAT`` isn't set explicitly.

    Plain (uncolored) output on ECS; colored output for local development.
    """
    return "plain" if running_on_ecs() else "colored"


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
    else:
        default_formatter = {
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
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
        },
        "root": {
            "handlers": ["default"],
            "level": log_level,
        },
        "loggers": {
            **suppressed_loggers,
        },
    }
