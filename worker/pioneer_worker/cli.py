"""Pioneer Square worker CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from . import __version__, config as config_mod
from .worker import Worker


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pioneer-worker",
        description="Run a Pioneer Square worker agent that connects to the backend over WebSocket.",
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to the TOML config file (default: ./pioneer-worker.toml).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--version", action="version", version=f"pioneer-worker {__version__}",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("pioneer_worker.cli")
    log.info("pioneer-worker CLI starting (log_level=%s)", args.log_level)

    try:
        cfg = config_mod.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Config load failed: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 2

    log.info("Config loaded from %s", cfg.config_path)
    worker = Worker(cfg)
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down")
        print("\nShutting down.", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
