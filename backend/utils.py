"""Small pure helpers shared across the backend.

Kept dependency-light so route modules can import without dragging in the
FastAPI app, the DB session helper, or anything else heavy.
"""

from __future__ import annotations

import base64
import json
import random
import string


def row_to_dict(obj) -> dict:
    """Convert a SQLAlchemy ORM model instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def generate_guild_id() -> str:
    """Random 6-char lowercase id used in guild URLs."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def worker_display_name(worker_id: str, hostname: str | None = None) -> str:
    """Render a worker id as ``HOST/PREFIX-SUFFIX`` for the UI."""
    raw = worker_id[2:].upper()
    split = 2 + sum(ord(c) for c in raw) % 3
    droid = f"{raw[:split]}-{raw[split:]}"
    if hostname:
        return f"{hostname[:3].upper()}/{droid}"
    return droid


def decode_claude_oauth_token(blob: str | None) -> str | None:
    """Extract the OAuth token from a stored claude_credentials blob.

    Only handles the modern ``base64(json({"oauth_token": "..."}))`` format
    written by ``claude setup-token``. The legacy tarball format is meant to
    be extracted to ``~/.claude`` on disk and can't be reduced to a single
    env var, so we ignore it here and let the worker handle it via the HTTP
    fetch path.
    """
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob)
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    token = payload.get("oauth_token") if isinstance(payload, dict) else None
    return token if isinstance(token, str) and token else None


def build_spawn_worker_env(
    *,
    guild_id: str,
    repos: list[str],
    worker_name: str | None,
    source_env: dict[str, str],
    claude_oauth_token: str | None = None,
) -> dict[str, str]:
    """Build the env dict for a spawned worker container.

    *claude_oauth_token* (e.g. fetched from the DB) wins over
    ``CLAUDE_CODE_OAUTH_TOKEN`` in *source_env* — the DB blob is the source
    of truth, refreshed every time a worker completes setup-token, while the
    host env var can drift stale.
    """
    env: dict[str, str] = {
        "PIONEER_BACKEND_URL": source_env.get("WORKER_BACKEND_URL", "http://backend:8000"),
        "PIONEER_GUILD_ID": guild_id,
        "PIONEER_REPOS": ",".join(repos),
    }
    gh_token = source_env.get("GITHUB_TOKEN", "")
    if gh_token:
        # PIONEER_GITHUB_TOKEN feeds the worker config loader; GITHUB_TOKEN is
        # what gh CLI inside the worker reads when opening PRs.
        env["PIONEER_GITHUB_TOKEN"] = gh_token
        env["GITHUB_TOKEN"] = gh_token
    # ANTHROPIC_API_KEY is intentionally NOT forwarded — that's the foreman's
    # API auth. Workers run the claude CLI under the user's OAuth subscription.
    oauth = claude_oauth_token or source_env.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    if worker_name:
        env["PIONEER_WORKER_NAME"] = worker_name
    log_level = source_env.get("WORKER_LOG_LEVEL")
    if log_level:
        env["PIONEER_WORKER_LOG_LEVEL"] = log_level
    return env
