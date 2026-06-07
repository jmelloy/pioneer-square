"""CLI entry point for the standalone foreman."""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load as load_config
from .foreman import Foreman
from .logging_config import setup_logging


def _build_foreman_parser(prog: str = "pioneer-square foreman") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        description="Standalone foreman agent for Pioneer Square.",
    )
    p.add_argument("--config", metavar="PATH", help="Path to TOML config file.")
    p.add_argument("--backend-url", metavar="URL", help="Override backend WebSocket URL.")
    p.add_argument("--guild-id", metavar="ID", help="Override guild ID.")
    p.add_argument("--model", metavar="MODEL", help="Override Claude model ID.")
    p.add_argument(
        "--backend-key",
        metavar="SECRET",
        help="Shared HMAC secret for JWT auth (matches PIONEER_FOREMAN_KEY on the backend).",
    )
    p.add_argument(
        "--auth-token",
        metavar="TOKEN",
        help="Static token fallback (member login_token or worker auth_token); "
        "used only when --backend-key is not set.",
    )
    p.add_argument(
        "--log-level",
        metavar="LEVEL",
        default=None,
        help="Logging level: DEBUG, INFO, WARNING (default: INFO).",
    )
    return p


def _run_foreman(args: argparse.Namespace) -> None:
    overrides: dict = {}
    if args.backend_url:
        overrides["backend_url"] = args.backend_url
    if args.guild_id:
        overrides["guild_id"] = args.guild_id
    if args.model:
        overrides["model"] = args.model
    if args.backend_key:
        overrides["backend_key"] = args.backend_key
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


def main() -> None:
    """Direct foreman entry point (``pioneer-square foreman``)."""
    args = _build_foreman_parser(prog="pioneer-square foreman").parse_args()
    _run_foreman(args)


def pioneer_square_main() -> None:
    """Unified ``pioneer-square`` entry point provided by the foreman package."""
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        print("usage: pioneer-square {foreman,worker} [options]")
        print("")
        print("subcommands:")
        print("  foreman    Run the standalone foreman agent.")
        print("  worker     Run a worker agent (requires pioneer-worker).")
        sys.exit(0 if (argv and argv[0] in ("-h", "--help")) else 2)

    subcommand, rest = argv[0], argv[1:]

    if subcommand == "foreman":
        args = _build_foreman_parser(prog="pioneer-square foreman").parse_args(rest)
        _run_foreman(args)
    elif subcommand == "worker":
        sys.stderr.write(
            "pioneer-square: the 'worker' subcommand requires the "
            "pioneer-worker package to be installed.\n"
        )
        sys.exit(1)
    else:
        sys.stderr.write(f"pioneer-square: unknown subcommand '{subcommand}'\n")
        sys.stderr.write("Run 'pioneer-square --help' for usage.\n")
        sys.exit(2)
