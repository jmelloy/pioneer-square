"""Configuration for the Pioneer Square worker.

Reads a TOML file (default: ``./pioneer-worker.toml``).
"""

from __future__ import annotations

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


@dataclass
class Config:
    backend_url: str
    guild_id: str
    repos: list[str] = field(default_factory=list)
    worker_id: Optional[str] = None
    worker_name: Optional[str] = None
    github_token: Optional[str] = None
    repos_dir: str = "src"
    work_dir: str = "worktrees"
    claude_path: str = "claude"
    codex_path: str = "codex"
    pi_path: str = "pi"
    pull_interval: float = 300.0
    claude_max_turns: int = 50
    max_agents: int = 4

    config_path: Path = field(default_factory=Path)

    @property
    def http_url(self) -> str:
        """Backend HTTP URL derived from backend_url."""
        parsed = urlparse(self.backend_url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme or "http")
        return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

    @property
    def ws_url(self) -> str:
        """Backend WebSocket URL for this guild."""
        parsed = urlparse(self.backend_url)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme or "ws")
        base = urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
        return f"{base}/ws/{self.guild_id}"


def _resolve_config_path(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return Path.cwd() / DEFAULT_CONFIG_NAME


def load(explicit_path: Optional[str] = None, overrides: Optional[dict] = None) -> Config:
    """Load config from TOML layered with env vars and overrides.

    *overrides* keys map directly to Config field names and take highest priority.
    If the config file is missing but overrides supply backend_url and guild_id,
    the file is treated as empty (all other values fall back to defaults).
    """
    overrides = overrides or {}
    cfg_path = _resolve_config_path(explicit_path)

    raw: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    else:
        has_url = overrides.get("backend_url") or os.environ.get("PIONEER_BACKEND_URL")
        has_sid = overrides.get("guild_id") or os.environ.get("PIONEER_GUILD_ID")
        if not (has_url and has_sid):
            raise FileNotFoundError(
                f"Worker config not found at {cfg_path}. "
                "Create one (see pioneer-worker.toml.example), pass --config, "
                "or supply --backend-url and --guild-id."
            )

    backend_url = overrides.get("backend_url") or raw.get("backend_url") or os.environ.get("PIONEER_BACKEND_URL")
    guild_id = overrides.get("guild_id") or raw.get("guild_id") or os.environ.get("PIONEER_GUILD_ID")
    if not backend_url:
        raise ValueError("backend_url is required (in config, --backend-url, or PIONEER_BACKEND_URL).")
    if not guild_id:
        raise ValueError("guild_id is required (in config, --guild-id, or PIONEER_GUILD_ID).")

    github_block = raw.get("github") or {}
    paths_block = raw.get("paths") or {}
    claude_block = raw.get("claude") or {}

    token = overrides.get("github_token")
    if token is None:
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

    _repos_env = os.environ.get("PIONEER_REPOS", "")
    _repos_from_env = [r.strip() for r in _repos_env.split(",") if r.strip()] if _repos_env else []

    return Config(
        backend_url=backend_url.rstrip("/"),
        guild_id=guild_id,
        repos=list(overrides.get("repos") or github_block.get("repos") or raw.get("repos") or _repos_from_env),
        worker_name=overrides.get("worker_name") or raw.get("worker_name") or os.environ.get("PIONEER_WORKER_NAME"),
        github_token=token,
        repos_dir=os.path.abspath(overrides.get("repos_dir") or paths_block.get("repos_dir", "/tmp/pioneer-repos")),
        work_dir=os.path.abspath(overrides.get("work_dir") or paths_block.get("work_dir", "/tmp/pioneer-work")),
        claude_path=overrides.get("claude_path") or paths_block.get("claude", "claude"),
        codex_path=overrides.get("codex_path") or paths_block.get("codex", "codex"),
        pi_path=overrides.get("pi_path") or paths_block.get("pi", "pi"),
        pull_interval=float(overrides.get("pull_interval") if overrides.get("pull_interval") is not None else raw.get("pull_interval", 300.0)),
        claude_max_turns=int(overrides.get("claude_max_turns") if overrides.get("claude_max_turns") is not None else claude_block.get("max_turns", 50)),
        max_agents=int(overrides.get("max_agents") if overrides.get("max_agents") is not None else raw.get("max_agents", 4)),
        config_path=cfg_path,
    )
