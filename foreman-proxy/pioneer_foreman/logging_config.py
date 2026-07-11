"""Logging configuration for the standalone foreman."""

import logging


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging with a consistent format."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
