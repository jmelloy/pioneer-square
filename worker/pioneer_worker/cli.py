"""Pioneer Square worker CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.config
import os
import sys

from . import __version__
from . import config as config_mod
from .logging_config import default_log_format, get_logging_config
from .mock_worker import MockWorker
from .worker import Worker


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pioneer-worker",
        description="Run a Pioneer Square worker agent that connects to the backend over WebSocket.",
    )
    parser.add_argument(
        "--config",
        "-c",
        help="Path to the TOML config file (default: ./pioneer-worker.toml).",
    )

    # Connection
    parser.add_argument("--backend-url", help="Backend WebSocket/HTTP base URL.")
    parser.add_argument("--guild-id", help="Guild ID to join.")

    # Identity
    parser.add_argument("--worker-name", help="Display name for this worker.")
    parser.add_argument(
        "--user",
        help=(
            "User identity to attribute this worker's actions to "
            "(numeric GitHub id or login; also reads WORKER_USER env var)."
        ),
    )

    # GitHub
    parser.add_argument("--github-token", help="GitHub personal access token.")
    parser.add_argument(
        "--repo",
        dest="repos",
        action="append",
        metavar="OWNER/REPO",
        help="Repo to operate on (may be repeated).",
    )

    # Paths
    parser.add_argument(
        "--repos-dir", help="Directory for cloned repos (default: /tmp/pioneer-repos)."
    )
    parser.add_argument(
        "--work-dir", help="Directory for git worktrees (default: /tmp/pioneer-work)."
    )
    parser.add_argument("--claude-path", help="Path to the claude executable (default: claude).")
    parser.add_argument("--codex-path", help="Path to the codex executable (default: codex).")
    parser.add_argument(
        "--codex-arg",
        dest="codex_args",
        action="append",
        help=(
            "Extra argument passed to `codex exec` before the prompt "
            "(may be repeated, e.g. --codex-arg --sandbox=workspace-write)."
        ),
    )
    parser.add_argument(
        "--skip-codex-doctor",
        dest="codex_doctor",
        action="store_false",
        default=None,
        help="Skip `codex doctor` startup diagnostics.",
    )
    parser.add_argument("--pi-path", help="Path to the pi executable (default: pi).")
    parser.add_argument(
        "--tools",
        help=(
            "Comma-separated list of tool runners to enable (e.g. claude,codex,pi). "
            "Each name is verified against its binary on PATH; tools not found are excluded "
            "with a warning. Omit to auto-detect all known tools."
        ),
    )
    parser.add_argument(
        "--install-tools",
        help=(
            "Comma-separated list of tool runners to install via npm at startup if missing "
            "from PATH (e.g. claude,codex,pi). Omit to fall back to --tools, or install all "
            "known tools if that's also unset. Pass an empty string to disable installation."
        ),
    )

    # Control API (drive/inspect a live worker without a frontend or foreman)
    parser.add_argument(
        "--api-port",
        type=int,
        help=(
            "Enable the HTTP control API on this port (lets you inject tasks and "
            "inspect the worker without a frontend/guild). Disabled if unset."
        ),
    )
    parser.add_argument(
        "--api-host",
        help="Bind host for the control API (default: 127.0.0.1).",
    )

    # Tuning
    parser.add_argument(
        "--pull-interval", type=float, help="Seconds between repo pulls (default: 300)."
    )
    parser.add_argument(
        "--claude-max-turns", type=int, help="Max turns for claude runs (unset by default)."
    )

    env_log_level = os.environ.get("PIONEER_WORKER_LOG_LEVEL", "").strip().upper()
    log_level_choices = ["DEBUG", "INFO", "WARNING", "ERROR"]
    default_log_level = env_log_level if env_log_level in log_level_choices else "INFO"
    parser.add_argument(
        "--log-level",
        default=default_log_level,
        choices=log_level_choices,
        help="Logging verbosity (default: INFO; can also set PIONEER_WORKER_LOG_LEVEL).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pioneer-worker {__version__}",
    )

    # Mock mode (for e2e tests). Skips all subprocess/git/claude work and
    # exposes an HTTP control API so Playwright tests can drive agent state
    # and task lifecycle deterministically.
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run as a mock worker for e2e tests (no real claude/git/gh).",
    )
    parser.add_argument(
        "--mock-api-port",
        type=int,
        default=9100,
        help="Port for the mock-mode HTTP control API (default: 9100).",
    )
    parser.add_argument(
        "--mock-api-host",
        default="127.0.0.1",
        help="Bind host for the mock-mode HTTP control API (default: 127.0.0.1).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    log_format = os.environ.get("LOG_FORMAT", default_log_format())
    logging.config.dictConfig(get_logging_config(log_level=args.log_level, log_format=log_format))
    log = logging.getLogger("pioneer_worker.cli")
    log.info("pioneer-worker CLI starting (log_level=%s)", args.log_level)

    overrides = {
        k: v
        for k, v in {
            "backend_url": args.backend_url,
            "guild_id": args.guild_id,
            "worker_name": args.worker_name,
            "user": args.user,
            "github_token": args.github_token,
            "repos": args.repos,
            "repos_dir": args.repos_dir,
            "work_dir": args.work_dir,
            "claude_path": args.claude_path,
            "codex_path": args.codex_path,
            "codex_args": args.codex_args,
            "codex_doctor": args.codex_doctor,
            "pi_path": args.pi_path,
            "tools": (
                [t.strip() for t in args.tools.split(",") if t.strip()]
                if args.tools is not None
                else None
            ),
            "install_tools": (
                [t.strip() for t in args.install_tools.split(",") if t.strip()]
                if args.install_tools is not None
                else None
            ),
            "pull_interval": args.pull_interval,
            "claude_max_turns": args.claude_max_turns,
            "api_port": args.api_port,
            "api_host": args.api_host,
        }.items()
        if v is not None
    }

    try:
        cfg = config_mod.load(args.config, overrides=overrides)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Config load failed: %s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return 2

    log.info("Config loaded from %s", cfg.config_path)

    if args.mock and not cfg.repos:
        # The mock worker needs to register against the backend, which expects a
        # non-empty repos list on the worker row. Inject a placeholder so the
        # registration call succeeds without forcing the test harness to set
        # one explicitly.
        cfg.repos = ["mock/mock"]
        log.info("Mock mode: defaulting repos to %s", cfg.repos)

    if not cfg.repos:
        msg = (
            "Worker cannot start: no repos configured. "
            "Set [github].repos in pioneer-worker.toml, pass --repo OWNER/REPO, "
            "or set PIONEER_REPOS."
        )
        log.error(msg)
        print(f"error: {msg}", file=sys.stderr)
        return 2

    if args.mock:
        log.info(
            "Starting in MOCK mode (HTTP API on %s:%d)",
            args.mock_api_host,
            args.mock_api_port,
        )
        worker = MockWorker(
            cfg,
            mock_api_port=args.mock_api_port,
            mock_api_host=args.mock_api_host,
        )
    else:
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
