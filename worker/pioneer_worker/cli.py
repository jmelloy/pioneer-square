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

    # Connection
    parser.add_argument("--backend-url", help="Backend WebSocket/HTTP base URL.")
    parser.add_argument("--guild-id", help="Guild ID to join.")

    # Identity
    parser.add_argument("--worker-name", help="Display name for this worker.")

    # GitHub
    parser.add_argument("--github-token", help="GitHub personal access token.")
    parser.add_argument(
        "--repo", dest="repos", action="append", metavar="OWNER/REPO",
        help="Repo to operate on (may be repeated).",
    )

    # Paths
    parser.add_argument("--repos-dir", help="Directory for cloned repos (default: /tmp/pioneer-repos).")
    parser.add_argument("--work-dir", help="Directory for git worktrees (default: /tmp/pioneer-work).")
    parser.add_argument("--claude-path", help="Path to the claude executable (default: claude).")
    parser.add_argument("--codex-path", help="Path to the codex executable (default: codex).")
    parser.add_argument("--pi-path", help="Path to the pi executable (default: pi).")

    # Tuning
    parser.add_argument("--pull-interval", type=float, help="Seconds between repo pulls (default: 300).")
    parser.add_argument("--claude-max-turns", type=int, help="Max turns for claude runs (default: 50).")

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

    overrides = {k: v for k, v in {
        "backend_url": args.backend_url,
        "guild_id": args.guild_id,
        "worker_name": args.worker_name,
        "github_token": args.github_token,
        "repos": args.repos,
        "repos_dir": args.repos_dir,
        "work_dir": args.work_dir,
        "claude_path": args.claude_path,
        "codex_path": args.codex_path,
        "pi_path": args.pi_path,
        "pull_interval": args.pull_interval,
        "claude_max_turns": args.claude_max_turns,
    }.items() if v is not None}

    try:
        cfg = config_mod.load(args.config, overrides=overrides)
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
