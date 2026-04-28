"""Configuration for the Pioneer Square worker.

Reads a TOML file (default: ``./pioneer-worker.toml``) and a sidecar JSON
state file (``.pioneer-worker.state.json`` next to the config) used to
persist runtime values like the worker id assigned by the backend.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


DEFAULT_CONFIG_NAME = "pioneer-worker.toml"
STATE_SUFFIX = ".state.json"


@dataclass
class Config:
    backend_url: str
    session_id: str
    repos: list[str] = field(default_factory=list)
    worker_id: Optional[str] = None
    worker_name: Optional[str] = None
    github_token: Optional[str] = None
    repos_dir: str = "src"
    work_dir: str = "worktrees"
    pull_interval: float = 300.0
    claude_max_turns: int = 50

    config_path: Path = field(default_factory=Path)
    state_path: Path = field(default_factory=Path)

    @property
    def http_url(self) -> str:
        """Backend HTTP URL derived from backend_url."""
        parsed = urlparse(self.backend_url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme or "http")
        return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

    @property
    def ws_url(self) -> str:
        """Backend WebSocket URL for this session."""
        parsed = urlparse(self.backend_url)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme or "ws")
        base = urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
        return f"{base}/ws/{self.session_id}"


def _resolve_config_path(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return Path.cwd() / DEFAULT_CONFIG_NAME


def load(explicit_path: Optional[str] = None) -> Config:
    """Load config from TOML, layered with optional sidecar state and env vars."""
    cfg_path = _resolve_config_path(explicit_path)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Worker config not found at {cfg_path}. "
            "Create one (see pioneer-worker.toml.example) or pass --config."
        )

    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)

    state_path = cfg_path.with_name(cfg_path.stem + STATE_SUFFIX)
    state: dict = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}

    backend_url = raw.get("backend_url") or os.environ.get("PIONEER_BACKEND_URL")
    session_id = raw.get("session_id") or os.environ.get("PIONEER_SESSION_ID")
    if not backend_url:
        raise ValueError("backend_url is required (in config or PIONEER_BACKEND_URL).")
    if not session_id:
        raise ValueError("session_id is required (in config or PIONEER_SESSION_ID).")

    github_block = raw.get("github") or {}
    paths_block = raw.get("paths") or {}
    claude_block = raw.get("claude") or {}

    token = github_block.get("token")
    if isinstance(token, str) and token.startswith("env:"):
        token = os.environ.get(token[4:].strip()) or None
    elif token is None:
        token = os.environ.get("PIONEER_GITHUB_TOKEN")

    cfg_dir = cfg_path.parent

    def _resolve(p: str) -> str:
        """Resolve *p* relative to the config file's directory if not absolute."""
        path = Path(p)
        return str(path if path.is_absolute() else cfg_dir / path)

    return Config(
        backend_url=backend_url.rstrip("/"),
        session_id=session_id,
        repos=list(github_block.get("repos") or raw.get("repos") or []),
        worker_id=state.get("worker_id") or raw.get("worker_id"),
        worker_name=raw.get("worker_name"),
        github_token=token,
        repos_dir=_resolve(paths_block.get("repos_dir", "src")),
        work_dir=_resolve(paths_block.get("work_dir", "worktrees")),
        pull_interval=float(raw.get("pull_interval", 300.0)),
        claude_max_turns=int(claude_block.get("max_turns", 50)),
        config_path=cfg_path,
        state_path=state_path,
    )


def save_worker_id(cfg: Config, worker_id: str) -> None:
    """Persist the assigned worker_id to the sidecar state file."""
    state: dict = {}
    if cfg.state_path.exists():
        try:
            state = json.loads(cfg.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
    state["worker_id"] = worker_id
    cfg.state_path.write_text(json.dumps(state, indent=2) + "\n")
    cfg.worker_id = worker_id
