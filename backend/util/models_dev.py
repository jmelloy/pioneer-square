"""Fetch and cache the models.dev provider/model catalog.

Fetched once at startup (or on first request) and refreshed every TTL seconds.
Falls back to a minimal static list if the fetch fails.

DB persistence: ``refresh_model_catalog_if_stale`` upserts all supported-provider
rows into ``model_catalog`` so the catalog survives restarts.  ``get_providers_from_db``
reads those rows back and reconstructs the provider list used by ``GET /api/models``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
_CACHE_TTL = 3600  # 1 hour

# Only surface Foreman-supported providers in the UI for now. models.dev uses
# "amazon-bedrock"; the rest of this app expects "bedrock" in config.
_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "amazon-bedrock": "bedrock",
    "bedrock": "bedrock",
}

# Human-readable display names for the normalized provider IDs.
_PROVIDER_DISPLAY_NAMES = {
    "anthropic": "Anthropic",
    "bedrock": "Amazon Bedrock",
}

# Minimal fallback so the UI always has something to show.
_FALLBACK_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "anthropic",
        "name": "Anthropic",
        "models": [
            {"id": "claude-opus-4-8", "name": "Claude Opus 4.8"},
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
            {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        ],
    },
    {
        "id": "bedrock",
        "name": "Amazon Bedrock",
        "models": [
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Bedrock)"},
        ],
    },
]

_cached_providers: list[dict[str, Any]] | None = None
_cache_fetched_at: float = 0.0


def _parse_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the models.dev API response into a flat list of provider dicts."""
    providers: list[dict[str, Any]] = []
    for provider_id, entry in data.items():
        normalized_provider_id = _PROVIDER_ALIASES.get(provider_id)
        if normalized_provider_id is None:
            continue
        if not isinstance(entry, dict):
            continue
        raw_models = entry.get("models", {})
        if not isinstance(raw_models, dict):
            continue
        models = [
            {"id": m.get("id", mid), "name": m.get("name", mid)}
            for mid, m in raw_models.items()
            if isinstance(m, dict)
        ]
        if not models:
            continue
        providers.append(
            {
                "id": normalized_provider_id,
                "name": entry.get("name", provider_id),
                "models": models,
            }
        )
    providers.sort(key=lambda p: p["name"].lower())
    return providers


async def fetch_providers(*, force: bool = False) -> list[dict[str, Any]]:
    """Return cached providers, refreshing from models.dev when the TTL has expired.

    On network/parse failure the previous cache (or the static fallback) is returned.
    """
    global _cached_providers, _cache_fetched_at

    now = time.monotonic()
    if not force and _cached_providers is not None and (now - _cache_fetched_at) < _CACHE_TTL:
        return _cached_providers

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(MODELS_DEV_URL)
            resp.raise_for_status()
            data = resp.json()
        providers = _parse_response(data)
        if providers:
            _cached_providers = providers
            _cache_fetched_at = now
            logger.info("models.dev: loaded %d providers", len(providers))
        else:
            logger.warning("models.dev: parsed 0 providers — keeping previous cache")
    except Exception as exc:
        logger.warning("models.dev: fetch failed (%s) — using fallback", exc)

    if _cached_providers is None:
        _cached_providers = _FALLBACK_PROVIDERS

    return _cached_providers


# ---------------------------------------------------------------------------
# DB persistence helpers
# ---------------------------------------------------------------------------


async def _upsert_catalog(
    db: AsyncSession,
    raw_data: dict[str, Any],
    bedrock_model_map: dict[str, str] | None = None,
) -> int:
    """Upsert supported-provider model rows from a raw models.dev API response.

    *bedrock_model_map* is an optional ``{short_id: bedrock_arn}`` mapping
    produced by the Bedrock enricher.  When provided, Bedrock rows are
    enriched with the canonical AWS model ID or inference-profile ARN.

    Returns the number of rows upserted. Does NOT commit — callers decide when
    to commit so they can batch with other writes.
    """
    from models import ModelCatalog
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    now = datetime.now(UTC)
    rows = []
    for provider_id, entry in raw_data.items():
        normalized = _PROVIDER_ALIASES.get(provider_id)
        if normalized is None:
            continue
        if not isinstance(entry, dict):
            continue
        raw_models = entry.get("models", {})
        if not isinstance(raw_models, dict):
            continue
        for mid, m in raw_models.items():
            if not isinstance(m, dict):
                continue
            capabilities = {k: v for k, v in m.items() if k not in ("id", "name")} or None
            short_id = m.get("id", mid)
            bedrock_model_id: str | None = None
            if normalized == "bedrock" and bedrock_model_map:
                bedrock_model_id = bedrock_model_map.get(short_id)
            rows.append(
                {
                    "provider": normalized,
                    "model_id": short_id,
                    "display_name": m.get("name", mid),
                    "capabilities": capabilities,
                    "raw": m,
                    "fetched_at": now,
                    "bedrock_model_id": bedrock_model_id,
                }
            )
    if not rows:
        return 0

    stmt = pg_insert(ModelCatalog).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "model_id"],
        set_={
            "display_name": stmt.excluded.display_name,
            "capabilities": stmt.excluded.capabilities,
            "raw": stmt.excluded.raw,
            "fetched_at": stmt.excluded.fetched_at,
            "bedrock_model_id": stmt.excluded.bedrock_model_id,
        },
    )
    await db.execute(stmt)
    return len(rows)


async def get_providers_from_db(db: AsyncSession) -> list[dict[str, Any]]:
    """Read the model catalog from DB and return in the same shape as fetch_providers().

    Returns an empty list if the catalog table is empty (caller should fall back
    to ``fetch_providers()``).
    """
    from models import ModelCatalog
    from sqlmodel import col, select

    rows = (await db.exec(select(ModelCatalog).order_by(col(ModelCatalog.provider)))).all()
    if not rows:
        return []

    providers: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.provider not in providers:
            providers[row.provider] = {
                "id": row.provider,
                "name": _PROVIDER_DISPLAY_NAMES.get(row.provider, row.provider),
                "models": [],
            }
        providers[row.provider]["models"].append({"id": row.model_id, "name": row.display_name})

    return sorted(providers.values(), key=lambda p: p["name"].lower())


async def refresh_model_catalog_if_stale(db: AsyncSession, *, max_age_hours: int = 24) -> bool:
    """Re-fetch models.dev and upsert into DB if the catalog is empty or stale.

    Staleness is determined by the oldest ``fetched_at`` in the table: if it is
    older than ``max_age_hours`` (or if the table is empty) a fresh fetch is
    performed and all rows are upserted.  The in-memory cache is also updated.

    Does NOT commit on its own — callers are responsible for committing after
    this returns ``True`` so they can batch with other writes.

    Returns ``True`` if a refresh was performed, ``False`` if the catalog was
    already fresh.
    """
    from models import ModelCatalog
    from sqlmodel import col, select

    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)

    oldest_fetched_at = (
        await db.exec(
            select(ModelCatalog.fetched_at).order_by(col(ModelCatalog.fetched_at).asc()).limit(1)
        )
    ).first()

    if oldest_fetched_at is not None and oldest_fetched_at > cutoff:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(MODELS_DEV_URL)
            resp.raise_for_status()
            data = resp.json()

        # Enrich Bedrock rows with canonical model IDs / inference-profile ARNs
        # when AWS credentials are available.  Failures are non-fatal.
        bedrock_map: dict[str, str] = {}
        has_bedrock = any(
            _PROVIDER_ALIASES.get(pid) == "bedrock"
            for pid, entry in data.items()
            if isinstance(entry, dict) and entry.get("models")
        )
        if has_bedrock:
            from util.bedrock_enricher import build_bedrock_model_map

            try:
                bedrock_map = await asyncio.to_thread(build_bedrock_model_map)
            except Exception as exc:
                logger.warning("models.dev: Bedrock enrichment failed (%s) — skipping", exc)

        count = await _upsert_catalog(db, data, bedrock_model_map=bedrock_map or None)
        providers = _parse_response(data)
        if providers:
            global _cached_providers, _cache_fetched_at
            _cached_providers = providers
            _cache_fetched_at = time.monotonic()
        logger.info("models.dev: refreshed catalog (%d rows upserted)", count)
        return True
    except Exception as exc:
        logger.warning("models.dev: catalog refresh failed (%s) — catalog unchanged", exc)
        return False
