"""CLI entry point for the standalone foreman."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load as load_config
from .foreman import Foreman
from .logging_config import setup_logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pioneer-foreman",
        description="Standalone foreman agent for Pioneer Square.",
    )
    p.add_argument("--config", metavar="PATH", help="Path to TOML config file.")
    p.add_argument("--backend-url", metavar="URL", help="Override backend WebSocket URL.")
    p.add_argument("--guild-id", metavar="ID", help="Override guild ID.")
    p.add_argument("--model", metavar="MODEL", help="Override Claude model ID.")
    p.add_argument("--auth-token", metavar="TOKEN", help="Override auth token.")
    p.add_argument(
        "--log-level",
        metavar="LEVEL",
        default=None,
        help="Logging level: DEBUG, INFO, WARNING (default: INFO).",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    overrides: dict = {}
    if args.backend_url:
        overrides["backend_url"] = args.backend_url
    if args.guild_id:
        overrides["guild_id"] = args.guild_id
    if args.model:
        overrides["model"] = args.model
    if args.auth_token:
        overrides["auth_token"] = args.auth_token
    if args.log_level:
        overrides["log_level"] = args.log_level

    try:
        config = load_config(args.config, overrides)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.log_level)

    foreman = Foreman(config)
    try:
        asyncio.run(foreman.run())
    except KeyboardInterrupt:
        pass
