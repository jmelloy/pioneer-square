"""Small pure helpers shared across the backend.

Kept dependency-light so route modules can import without dragging in the
FastAPI app, the DB session helper, or anything else heavy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import re
import string
import time
import unicodedata

_VOWELS = frozenset("aeiou")
_MIN_LEN = 5
_TARGET_LEN = 8
_SUFFIX_LEN = 4


def row_to_dict(obj) -> dict:
    """Convert a SQLAlchemy ORM model instance to a plain dict."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _slugify(name: str) -> str:
    """Lowercase, normalize unicode, replace non-alphanumeric runs with '-'."""
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    lowered = normalized.lower()
    slugged = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slugged.strip("-")


def _strip_vowels(s: str) -> str:
    """Remove vowels, keeping consonants and digits and hyphens."""
    return "".join(c for c in s if c not in _VOWELS)


def _candidate_from_name(name: str) -> str:
    """Derive a deterministic slug from a guild name."""
    slug = _slugify(name)
    if not slug:
        return ""

    devoweled = _strip_vowels(slug)

    # If devoweling left nothing or below minimum, fall back to the slug.
    if len(devoweled) < _MIN_LEN:
        base = slug
    else:
        base = devoweled

    # Trim from end until we're at or below the target length.
    while len(base) > _TARGET_LEN:
        # Don't trim past the minimum.
        if len(base) <= _MIN_LEN:
            break
        base = base[:-1].rstrip("-")

    return base or slug


def generate_guild_id(name: str = "", existing_ids: set[str] | None = None) -> str:
    """Return a slug-based guild ID derived from *name*, unique among *existing_ids*.

    Algorithm:
    1. Slugify the name (lowercase, replace non-alphanum with '-').
    2. Remove vowels from the slug.
    3. If the result is < _MIN_LEN chars, fall back to the full slug.
    4. Trim from the end to _TARGET_LEN, never below _MIN_LEN.
    5. If the candidate collides with an existing ID, append a random suffix.
    6. If no name is given, generate a fully random ID (legacy fallback).
    """
    if existing_ids is None:
        existing_ids = set()

    candidate = _candidate_from_name(name) if name else ""

    if not candidate:
        # No usable name — generate a random ID.
        while True:
            rid = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
            if rid not in existing_ids:
                return rid

    if candidate not in existing_ids:
        return candidate

    # Collision: append a random suffix.
    while True:
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=_SUFFIX_LEN))
        unique = f"{candidate}-{suffix}"
        if unique not in existing_ids:
            return unique


# ---------------------------------------------------------------------------
# MIRROR: format_worker_id is intentionally duplicated in
#   frontend/src/utils/format.ts as ``formatWorkerId``.
# Both implementations MUST be kept in sync.
#
# Transformation rules:
#   1. Strip the "w-" prefix (removeprefix is a no-op if absent).
#   2. Find the first digit in the remaining string.
#   3. If a digit exists at position > 0, split there:
#        LEFT  = everything before the first digit (uppercased)
#        RIGHT = everything from the first digit onward (uppercased)
#        result = LEFT + "-" + RIGHT
#   4. Otherwise return the whole string uppercased (no hyphen inserted).
#
# Examples:  w-vd3566 → VD-3566 | w-ab1234 → AB-1234 | w-x9 → X-9
# Edge cases:
#   - All-digit suffix (e.g. w-1234): m.start()==0, returns "1234".
#   - No-digit suffix (e.g. w-abc):   no match,     returns "ABC".
#   - No w- prefix (bare ID):         no-op strip,  still formatted.
# ---------------------------------------------------------------------------
def format_worker_id(worker_id: str) -> str:
    """Format a worker ID in droid style: ``w-vd3566`` → ``VD-3566``.

    Strips the ``w-`` prefix, splits at the first digit boundary, uppercases
    both parts, and joins with a hyphen.  Examples: ``w-ab1234`` → ``AB-1234``,
    ``w-x9`` → ``X-9``, ``w-g2otus`` → ``G-2OTUS``.

    Input is expected to be a ``w-<slug>`` worker ID, but the function degrades
    gracefully for bare slugs (no prefix) or all-digit/all-letter slugs.
    """
    bare = worker_id.removeprefix("w-")
    m = re.search(r"\d", bare)
    if m and m.start() > 0:
        return f"{bare[: m.start()].upper()}-{bare[m.start() :].upper()}"
    return bare.upper()


def worker_display_name(worker_id: str, hostname: str | None = None) -> str:
    """Render a worker ID as ``hostname/VD-3566``, or just ``VD-3566`` when no hostname.

    The hostname, when present, is prepended with a ``/`` separator so operators
    can tell workers on different machines apart at a glance.
    """
    droid = format_worker_id(worker_id)
    if hostname:
        return f"{hostname}/{droid}"
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


# ---------------------------------------------------------------------------
# Foreman JWT helpers (HS256, stdlib-only)
# ---------------------------------------------------------------------------

_FOREMAN_JWT_SUB = "pioneer-foreman"
_FOREMAN_JWT_TTL = 3600  # seconds; foreman refreshes before expiry


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def make_foreman_jwt(guild_id: str, secret: str, ttl: int = _FOREMAN_JWT_TTL) -> str:
    """Create a short-lived HS256 JWT for Foreman REST helper endpoints.

    The signer and backend (via ``PIONEER_FOREMAN_KEY`` env var) must share the
    same *secret*. The token is valid for *ttl* seconds (default 1 hour).
    """
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url_encode(
        json.dumps(
            {
                "sub": _FOREMAN_JWT_SUB,
                "slug": guild_id,
                "iat": now,
                "exp": now + ttl,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    sig = _b64url_encode(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())
    return f"{signing_input}.{sig}"


def verify_foreman_jwt(token: str, secret: str, guild_id: str) -> bool:
    """Return True iff *token* is a valid foreman JWT for *guild_id*.

    Checks: HS256 signature, ``sub`` == ``"pioneer-foreman"``, ``guild_id``
    matches, and the token has not expired.  Returns False (never raises) on
    any validation failure so callers can safely fall through to other auth checks.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        signing_input = f"{parts[0]}.{parts[1]}"
        expected_sig = _b64url_encode(
            hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected_sig, parts[2]):
            return False
        payload = json.loads(_b64url_decode(parts[1]))
        if payload.get("sub") != _FOREMAN_JWT_SUB:
            return False
        if payload.get("slug") != guild_id:
            return False
        exp = payload.get("exp", 0)
        if time.time() > exp:
            return False
        return True
    except Exception:
        return False


def build_spawn_worker_env(
    *,
    guild_id: str,
    repos: list[str],
    worker_name: str | None,
    source_env: dict[str, str],
    claude_oauth_token: str | None = None,
    worker_id: str | None = None,
    auth_token: str | None = None,
    agent_count: int | None = None,
    tools: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the env dict for a spawned worker container.

    *claude_oauth_token* (e.g. fetched from the DB) wins over
    ``CLAUDE_CODE_OAUTH_TOKEN`` in *source_env* — the DB blob is the source
    of truth, refreshed every time a worker completes setup-token, while the
    host env var can drift stale.

    *worker_id* and *auth_token* — when provided (pre-registered by the
    foreman), the worker process skips its own self-registration and uses
    these credentials directly.  This lets callers know the worker_id before
    the container starts.

    *extra_env* — additional key/value pairs supplied by the user at spawn
    time (e.g. via the UI).  These are merged in first so that Pioneer's own
    required vars (PIONEER_GUILD_ID, PIONEER_AUTH_TOKEN, etc.) always win.
    """
    if extra_env:
        # Intentionally defensive: callers that bypass the Pydantic model (e.g. tests,
        # internal callers) still get consistent validation with a clear error message.
        for key in extra_env:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ValueError(
                    f"Invalid env var key: {key!r}. Keys must match ^[A-Za-z_][A-Za-z0-9_]*$"
                )
    # User-supplied extras go in first; Pioneer's required vars overwrite them.
    env: dict[str, str] = dict(extra_env) if extra_env else {}
    env.update(
        {
            "PIONEER_BACKEND_URL": source_env.get("WORKER_BACKEND_URL", "http://backend:8000"),
            "PIONEER_GUILD_ID": guild_id,
            "PIONEER_REPOS": ",".join(repos),
        }
    )
    public_url = source_env.get("FRONTEND_URL", "").rstrip("/")
    if public_url:
        env["PIONEER_FRONTEND_URL"] = public_url
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
    if worker_id:
        env["PIONEER_WORKER_ID"] = worker_id
    if auth_token:
        env["PIONEER_AUTH_TOKEN"] = auth_token
    if agent_count is not None:
        env["PIONEER_MAX_AGENTS"] = str(agent_count)
    if tools:
        env["PIONEER_TOOLS"] = ",".join(tools)
    # S3 session-log sync — forward bucket, prefix, interval, and AWS creds so
    # spawned workers can upload without needing their own config file entries.
    for _key in (
        "PIONEER_S3_BUCKET",
        "PIONEER_S3_PREFIX",
        "PIONEER_S3_SYNC_INTERVAL",
        "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        _val = source_env.get(_key, "")
        if _val:
            env[_key] = _val
    return env
