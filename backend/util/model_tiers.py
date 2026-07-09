"""Task-type → model tier mapping.

Defines three tiers (cheap, standard, powerful) and the rules that map a
task's ``phase`` and ``tool`` fields to the appropriate tier.  A concrete
model ID can then be resolved from the persisted models.dev catalog via
``get_model_for_tier()``.

Priority order for ``select_model_tier()``:
  1. ``complexity_hint`` — explicit caller override, highest priority.
  2. Tool hard tier — tools like ``codex`` always require a specific tier
     regardless of phase.
  3. Phase tier — plan/execute → standard, issue/review → cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Tier constants
# ---------------------------------------------------------------------------

TIER_CHEAP: str = "cheap"
TIER_STANDARD: str = "standard"
TIER_POWERFUL: str = "powerful"

_VALID_TIERS: frozenset[str] = frozenset({TIER_CHEAP, TIER_STANDARD, TIER_POWERFUL})

# Numeric ordering used for min/max comparisons.
_TIER_ORDER: dict[str, int] = {TIER_CHEAP: 0, TIER_STANDARD: 1, TIER_POWERFUL: 2}

# ---------------------------------------------------------------------------
# Phase → tier  (used when the tool has no hard tier requirement)
# ---------------------------------------------------------------------------

_PHASE_TIERS: dict[str, str] = {
    "issue": TIER_CHEAP,
    "plan": TIER_STANDARD,
    "execute": TIER_STANDARD,
    "review": TIER_CHEAP,
}

# ---------------------------------------------------------------------------
# Tool tier config
# ---------------------------------------------------------------------------

# Tools listed here have a hard tier requirement that overrides the phase tier.
# All other tools fall back to the phase-based tier.
_TOOL_HARD_TIERS: dict[str, str] = {
    "codex": TIER_POWERFUL,
}

# Default tier for tools without a hard requirement (used in documentation /
# edge-case fallbacks — actual dispatch goes through the phase-tier path).
_TOOL_DEFAULT_TIERS: dict[str, str] = {
    "claude": TIER_STANDARD,
    "pi": TIER_STANDARD,  # configurable; complexity_hint can override
}

# ---------------------------------------------------------------------------
# Per-provider model preference lists (most-preferred first) for each tier.
# ---------------------------------------------------------------------------

_TIER_MODEL_PREFERENCES: dict[str, dict[str, list[str]]] = {
    "anthropic": {
        TIER_CHEAP: [
            "claude-haiku-4-5-20251001",
            "claude-haiku-4-5",
            "claude-sonnet-4-6",
        ],
        TIER_STANDARD: [
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ],
        TIER_POWERFUL: [
            "claude-opus-4-8",
            "claude-sonnet-4-6",
        ],
    },
    "bedrock": {
        TIER_CHEAP: [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
        ],
        TIER_STANDARD: [
            "claude-sonnet-4-6",
        ],
        TIER_POWERFUL: [
            "claude-sonnet-4-6",
        ],
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_model_tier(
    phase: str | None,
    tool: str | None,
    complexity_hint: str | None = None,
) -> str:
    """Return the appropriate tier string for a task.

    Args:
        phase: Task phase — ``"plan"``, ``"execute"``, or ``"review"``.
               Unknown values fall back to ``TIER_STANDARD``.
        tool: Runner name — ``"claude"``, ``"codex"``, or ``"pi"``.
              Unknown values fall back to the phase tier.
        complexity_hint: Explicit tier override supplied by the caller
                         (``"cheap"``, ``"standard"``, or ``"powerful"``).
                         Invalid values are silently ignored.

    Returns:
        One of ``TIER_CHEAP``, ``TIER_STANDARD``, or ``TIER_POWERFUL``.
    """
    if complexity_hint in _VALID_TIERS:
        return complexity_hint

    # Tools with hard tier requirements ignore phase.
    if tool in _TOOL_HARD_TIERS:
        return _TOOL_HARD_TIERS[tool]

    # All other tools: let the phase drive the tier.
    return _PHASE_TIERS.get(phase or "", TIER_STANDARD)


def get_model_for_tier(
    tier: str,
    provider: str,
    catalog: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return the best available model ID for *tier* + *provider*.

    Walks the per-provider preference list for *tier* and returns the first
    entry that appears in *catalog*.  Falls back to the first preference when
    the catalog is absent or the provider is not listed there.

    Args:
        tier: One of ``TIER_CHEAP``, ``TIER_STANDARD``, ``TIER_POWERFUL``.
        provider: Normalized provider ID, e.g. ``"anthropic"`` or ``"bedrock"``.
        catalog: Optional provider list in the same shape returned by
                 ``fetch_providers()`` / ``get_providers_from_db()``.  When
                 ``None`` the module-level in-memory cache from
                 ``util.models_dev`` is used if populated; if neither is
                 available the first preference is returned without validation.

    Returns:
        A model ID string, or ``None`` if no preferences are defined for the
        given *provider* + *tier* combination.
    """
    prefs = _TIER_MODEL_PREFERENCES.get(provider, {}).get(tier)
    if not prefs:
        return None

    effective_catalog = catalog
    if effective_catalog is None:
        try:
            from util import models_dev as _md

            effective_catalog = _md._cached_providers
        except Exception:
            effective_catalog = None

    if not effective_catalog:
        return prefs[0]

    # Collect model IDs available for this provider in the catalog.
    available: set[str] = set()
    for p in effective_catalog:
        if p.get("id") == provider:
            available = {m["id"] for m in p.get("models", [])}
            break

    for model_id in prefs:
        if model_id in available:
            return model_id

    # No preference matched catalog — return the top preference as default.
    return prefs[0]
