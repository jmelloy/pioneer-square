"""Dispatcher for the unified ``pioneer`` CLI.

Usage::

    pioneer serve   [--host H] [--port P] [--log-level L]
    pioneer foreman [...standalone-foreman args...]
    pioneer worker  [...worker args...]

Each subcommand resolves the repository root (so the sibling ``backend/``,
``foreman-proxy/`` and ``worker/`` source trees can be imported), wires up
``sys.path`` for that mode, and delegates to the runtime's existing entry
point. The repo root is ``cli/``'s parent, overridable with ``PIONEER_ROOT``
(set in the Docker images).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _resolve_root() -> Path:
    """Return the repository root that holds backend/, foreman-proxy/, worker/, cli/."""
    env = os.environ.get("PIONEER_ROOT")
    if env:
        return Path(env).resolve()
    # cli/pioneer_cli/main.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _prepend_path(p: Path) -> None:
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _run_serve(argv: list[str]) -> int:
    """Launch the FastAPI backend, mirroring ``python backend/main.py``."""
    parser = argparse.ArgumentParser(prog="pioneer serve", description="Run the HTTP backend.")
    parser.add_argument("--host", default=os.environ.get("PIONEER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PIONEER_PORT", "8000")))
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Logging verbosity (default: INFO; also reads LOG_LEVEL).",
    )
    parser.add_argument(
        "--reload", action="store_true", help="Enable uvicorn auto-reload (development)."
    )
    args = parser.parse_args(argv)

    backend_dir = _resolve_root() / "backend"
    if not backend_dir.is_dir():
        print(f"error: backend directory not found at {backend_dir}", file=sys.stderr)
        return 2

    # The backend imports its modules as top-level names (``from events import``,
    # ``main:app``, ``foreman``...) and resolves Alembic/static paths from
    # its own directory, so run it exactly as ``python main.py`` would.
    _prepend_path(backend_dir)
    os.chdir(backend_dir)

    import uvicorn  # noqa: PLC0415
    from logging_config import default_log_format, get_logging_config  # noqa: PLC0415

    log_level = args.log_level.upper()
    log_format = os.environ.get("LOG_FORMAT", default_log_format())
    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=True,
        log_level=log_level.lower(),
        log_config=get_logging_config(log_level, log_format),
    )
    return 0


def _run_worker(argv: list[str]) -> int:
    """Launch a worker agent (delegates to pioneer_worker.cli.main)."""
    _prepend_path(_resolve_root() / "worker")
    from pioneer_worker.cli import main as worker_main  # noqa: PLC0415

    return worker_main(argv)


def _run_foreman(argv: list[str]) -> int:
    """Launch the standalone Foreman API proxy (delegates to pioneer_foreman.cli.main)."""
    root = _resolve_root()
    # foreman-proxy/ holds the pioneer_foreman package; root makes ``backend.foreman.llm``
    # importable from source without importing the backend runtime.
    _prepend_path(root / "foreman-proxy")
    _prepend_path(root)
    from pioneer_foreman.cli import main as foreman_main  # noqa: PLC0415

    # pioneer_foreman's main() parses sys.argv directly, so present it a clean argv.
    sys.argv = ["pioneer-foreman", *argv]
    foreman_main()
    return 0


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

_MODES = {
    "serve": _run_serve,
    "worker": _run_worker,
    "foreman": _run_foreman,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: pioneer {serve|foreman|worker} [args...]\n\n"
            "  serve     run the HTTP backend (FastAPI)\n"
            "  foreman   run the standalone Foreman API proxy\n"
            "  worker    run a worker agent\n\n"
            "Run 'pioneer <mode> --help' for mode-specific options.",
            file=sys.stderr,
        )
        return 0 if argv else 2
    mode, rest = argv[0], argv[1:]
    runner = _MODES.get(mode)
    if runner is None:
        print(
            f"error: unknown mode {mode!r} (expected one of: {', '.join(_MODES)})",
            file=sys.stderr,
        )
        return 2
    return runner(rest)


def worker_alias() -> int:
    """Backward-compatible ``pioneer-worker`` entry point."""
    return _run_worker(sys.argv[1:])


def foreman_alias() -> int:
    """Backward-compatible ``pioneer-foreman`` entry point."""
    return _run_foreman(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
