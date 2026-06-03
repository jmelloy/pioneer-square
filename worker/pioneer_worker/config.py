"""Configuration for the Pioneer Square worker.

Reads a TOML file (default: ``./pioneer-worker.toml``).
"""

from __future__ import annotations

import logging
import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "pioneer-worker.toml"


@dataclass
class Config:
    backend_url: str
    guild_id: str
    repos: list[str] = field(default_factory=list)
    # GitHub org name (e.g. "jmelloy"). When set, the worker is eligible for
    # any task targeting <org>/* and will clone repos lazily on first use.
    # Can be used alongside repos (static list) or instead of it.
    org: str | None = None
    # Pre-assigned by the foreman's spawn_worker tool.  When both are set the
    # worker skips self-registration and uses these credentials directly.
    worker_id: str | None = None
    worker_name: str | None = None
    # Bearer token issued by the backend at registration; required for fetching
    # guild secrets (Claude credentials, GitHub token). Populated by Worker.run
    # after _register and held in memory only.  Also supplied via
    # PIONEER_AUTH_TOKEN when the foreman pre-registers the worker.
    auth_token: str | None = None
    github_token: str | None = None
    # Identity of the human this worker runs on behalf of.
    # Either a numeric GitHub user id (text) or a github_login.
    # The backend resolves it to a users.id row and stores workers.user_id.
    user: str | None = None
    repos_dir: str = "src"
    work_dir: str = "worktrees"
    claude_path: str = "claude"
    codex_path: str = "codex"
    codex_args: list[str] = field(default_factory=list)
    codex_doctor: bool = True
    # OpenAI API key for Codex tasks. Falls back to OPENAI_API_KEY env var at
    # startup; set here to avoid exposing the secret in the process environment
    # before the worker has a chance to forward it.
    openai_api_key: str | None = None
    pi_path: str = "pi"
    pi_model: str | None = None
    pi_provider: str | None = None
    pull_interval: float = 300.0
    claude_max_turns: int = 50
    max_agents: int = 4
    # Optional public-facing backend URL used when registering GitHub webhooks.
    # ``backend_url`` may point at an internal address GitHub can't reach
    # (e.g. ``http://backend:8000`` in Docker); set ``public_backend_url`` to
    # the externally-reachable URL (e.g. an ngrok / Cloudflare tunnel) and
    # webhook registration will target it. Defaults to ``backend_url``.
    public_backend_url: str | None = None

    # Optional HTTP control API for driving/inspecting a live worker without a
    # frontend or the foreman. Disabled unless ``api_port`` is set (via
    # [api].port, --api-port, or PIONEER_API_PORT). Bind to localhost only.
    api_port: int | None = None
    api_host: str = "127.0.0.1"

    config_path: Path = field(default_factory=Path)

    @property
    def http_url(self) -> str:
        """Backend HTTP URL derived from backend_url."""
        parsed = urlparse(self.backend_url)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme or "http")
        return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

    @property
    def webhook_target_url(self) -> str:
        """Public URL GitHub should POST webhook deliveries to for this guild."""
        base = (self.public_backend_url or self.http_url).rstrip("/")
        return f"{base}/webhooks/github/{self.guild_id}"

    @property
    def ws_url(self) -> str:
        """Backend WebSocket URL for this guild."""
        parsed = urlparse(self.backend_url)
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme or "ws")
        base = urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
        return f"{base}/ws/{self.guild_id}"


def _resolve_config_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return Path.cwd() / DEFAULT_CONFIG_NAME


def load(explicit_path: str | None = None, overrides: dict | None = None) -> Config:
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

    backend_url = (
        overrides.get("backend_url")
        or raw.get("backend_url")
        or os.environ.get("PIONEER_BACKEND_URL")
    )
    guild_id = (
        overrides.get("guild_id") or raw.get("guild_id") or os.environ.get("PIONEER_GUILD_ID")
    )
    if not backend_url:
        raise ValueError(
            "backend_url is required (in config, --backend-url, or PIONEER_BACKEND_URL)."
        )
    if not guild_id:
        raise ValueError("guild_id is required (in config, --guild-id, or PIONEER_GUILD_ID).")

    github_block = raw.get("github") or {}
    paths_block = raw.get("paths") or {}
    claude_block = raw.get("claude") or {}
    codex_block = raw.get("codex") or {}
    api_block = raw.get("api") or {}
    pi_block = raw.get("pi") or {}

    _api_port = (
        overrides.get("api_port")
        if overrides.get("api_port") is not None
        else api_block.get("port")
        if api_block.get("port") is not None
        else os.environ.get("PIONEER_API_PORT")
    )
    _api_port = int(_api_port) if _api_port is not None and _api_port != "" else None
    _api_host = (
        overrides.get("api_host")
        or api_block.get("host")
        or os.environ.get("PIONEER_API_HOST")
        or "127.0.0.1"
    )

    token = overrides.get("github_token")
    if token is None:
        token = github_block.get("token")
        if isinstance(token, str) and token.startswith("env:"):
            token = os.environ.get(token[4:].strip()) or None
        elif token is None:
            # GITHUB_TOKEN fallback supports backend-spawned containers, which
            # mount no TOML and inherit the var verbatim from the backend env.
            token = os.environ.get("PIONEER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")

    cfg_dir = cfg_path.parent

    def _resolve(p: str) -> str:
        """Resolve *p* relative to the config file's directory if not absolute."""
        path = Path(p)
        return str(path if path.is_absolute() else cfg_dir / path)

    _repos_env = os.environ.get("PIONEER_REPOS", "")
    _repos_from_env = [r.strip() for r in _repos_env.split(",") if r.strip()] if _repos_env else []
    _codex_args_env = os.environ.get("PIONEER_CODEX_ARGS", "")
    _codex_args = (
        overrides.get("codex_args")
        if overrides.get("codex_args") is not None
        else codex_block.get("args")
        if codex_block.get("args") is not None
        else shlex.split(_codex_args_env)
        if _codex_args_env
        else []
    )
    if isinstance(_codex_args, str):
        _codex_args = shlex.split(_codex_args)
    if not isinstance(_codex_args, list) or not all(isinstance(arg, str) for arg in _codex_args):
        raise ValueError("codex args must be a list of strings.")

    _openai_api_key = overrides.get("openai_api_key")
    if _openai_api_key is None:
        raw_key = codex_block.get("api_key")
        if isinstance(raw_key, str) and raw_key.startswith("env:"):
            _var_name = raw_key[4:].strip()
            if not _var_name:
                raise ValueError("env: directive has a blank variable name")
            _env_val = os.environ.get(_var_name)
            if _env_val == "":
                logger.warning(
                    "[codex] api_key references env:%r but the variable is set to an empty"
                    " string; treating as absent",
                    _var_name,
                )
            else:
                _openai_api_key = _env_val  # None if var is absent, key string if present
        elif isinstance(raw_key, str) and raw_key.strip():
            _openai_api_key = raw_key.strip()
    if _openai_api_key is None:
        _env_val = os.environ.get("OPENAI_API_KEY")
        if _env_val == "":
            raise ValueError(
                "OPENAI_API_KEY is set to an empty string. "
                "Provide a valid key or unset the variable to skip Codex support."
            )
        _openai_api_key = _env_val  # None if var is absent, key string if present

    _max_agents_env = os.environ.get("PIONEER_MAX_AGENTS")
    if _max_agents_env:
        try:
            _default_max_agents: int = int(_max_agents_env)
        except ValueError:
            logger.warning("Invalid PIONEER_MAX_AGENTS=%r, using default 4", _max_agents_env)
            _default_max_agents = 4
    else:
        _default_max_agents = 4

    _max_agents_val = (
        overrides.get("max_agents")
        if overrides.get("max_agents") is not None
        else raw.get("max_agents", _default_max_agents)
    )

    return Config(
        backend_url=backend_url.rstrip("/"),
        guild_id=guild_id,
        repos=list(
            overrides.get("repos")
            or github_block.get("repos")
            or raw.get("repos")
            or _repos_from_env
        ),
        org=overrides.get("org")
        or github_block.get("org")
        or os.environ.get("PIONEER_ORG")
        or None,
        worker_id=overrides.get("worker_id") or os.environ.get("PIONEER_WORKER_ID") or None,
        auth_token=overrides.get("auth_token") or os.environ.get("PIONEER_AUTH_TOKEN") or None,
        worker_name=overrides.get("worker_name")
        or raw.get("worker_name")
        or os.environ.get("PIONEER_WORKER_NAME"),
        user=overrides.get("user")
        or raw.get("user")
        or os.environ.get("WORKER_USER")
        or os.environ.get("PIONEER_WORKER_USER"),
        github_token=token,
        repos_dir=os.path.abspath(
            overrides.get("repos_dir") or paths_block.get("repos_dir", "/tmp/pioneer-repos")
        ),
        work_dir=os.path.abspath(
            overrides.get("work_dir") or paths_block.get("work_dir", "/tmp/pioneer-work")
        ),
        claude_path=overrides.get("claude_path") or paths_block.get("claude", "claude"),
        codex_path=overrides.get("codex_path") or paths_block.get("codex", "codex"),
        codex_args=list(_codex_args),
        codex_doctor=bool(
            overrides.get("codex_doctor")
            if overrides.get("codex_doctor") is not None
            else codex_block.get("doctor", True)
        ),
        openai_api_key=_openai_api_key,
        pi_path=overrides.get("pi_path") or paths_block.get("pi", "pi"),
        pi_model=overrides.get("pi_model")
        or pi_block.get("model")
        or os.environ.get("PIONEER_PI_MODEL")
        or None,
        pi_provider=overrides.get("pi_provider")
        or pi_block.get("provider")
        or os.environ.get("PIONEER_PI_PROVIDER")
        or None,
        pull_interval=float(
            overrides.get("pull_interval")
            if overrides.get("pull_interval") is not None
            else raw.get("pull_interval", 300.0)
        ),
        claude_max_turns=int(
            overrides.get("claude_max_turns")
            if overrides.get("claude_max_turns") is not None
            else claude_block.get("max_turns", 50)
        ),
        max_agents=int(_max_agents_val),
        public_backend_url=overrides.get("public_backend_url")
        or raw.get("public_backend_url")
        or os.environ.get("PIONEER_FRONTEND_URL"),
        api_port=_api_port,
        api_host=_api_host,
        config_path=cfg_path,
    )
