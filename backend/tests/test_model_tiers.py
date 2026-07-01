"""Tests for the task-type → model tier mapping."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from util.model_tiers import (
    TIER_CHEAP,
    TIER_POWERFUL,
    TIER_STANDARD,
    get_model_for_tier,
    select_model_tier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_CATALOG = [
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


# ---------------------------------------------------------------------------
# select_model_tier — phase-based mapping
# ---------------------------------------------------------------------------


def test_select_plan_phase_is_standard():
    assert select_model_tier("plan", "claude") == TIER_STANDARD


def test_select_execute_phase_is_standard():
    assert select_model_tier("execute", "claude") == TIER_STANDARD


def test_select_review_phase_is_cheap():
    assert select_model_tier("review", "claude") == TIER_CHEAP


def test_select_review_phase_pi_is_cheap():
    assert select_model_tier("review", "pi") == TIER_CHEAP


def test_select_execute_phase_pi_is_standard():
    assert select_model_tier("execute", "pi") == TIER_STANDARD


# ---------------------------------------------------------------------------
# select_model_tier — tool hard-tier overrides
# ---------------------------------------------------------------------------


def test_select_codex_tool_is_powerful_in_execute():
    assert select_model_tier("execute", "codex") == TIER_POWERFUL


def test_select_codex_tool_is_powerful_in_plan():
    assert select_model_tier("plan", "codex") == TIER_POWERFUL


def test_select_codex_tool_overrides_review_phase():
    # codex always requires powerful, even in review phase
    assert select_model_tier("review", "codex") == TIER_POWERFUL


# ---------------------------------------------------------------------------
# select_model_tier — complexity_hint override
# ---------------------------------------------------------------------------


def test_select_complexity_hint_cheap_overrides_codex():
    assert select_model_tier("execute", "codex", complexity_hint=TIER_CHEAP) == TIER_CHEAP


def test_select_complexity_hint_powerful_overrides_review():
    assert select_model_tier("review", "claude", complexity_hint=TIER_POWERFUL) == TIER_POWERFUL


def test_select_complexity_hint_standard():
    assert select_model_tier("plan", "claude", complexity_hint=TIER_STANDARD) == TIER_STANDARD


def test_select_invalid_complexity_hint_is_ignored():
    # Invalid hint → falls through to normal resolution
    assert select_model_tier("review", "claude", complexity_hint="ultra") == TIER_CHEAP


def test_select_none_complexity_hint_ignored():
    assert select_model_tier("review", "claude", complexity_hint=None) == TIER_CHEAP


# ---------------------------------------------------------------------------
# select_model_tier — unknown/None inputs
# ---------------------------------------------------------------------------


def test_select_unknown_phase_defaults_to_standard():
    assert select_model_tier("unknown_phase", "claude") == TIER_STANDARD


def test_select_none_phase_defaults_to_standard():
    assert select_model_tier(None, "claude") == TIER_STANDARD


def test_select_unknown_tool_uses_phase_tier():
    assert select_model_tier("review", "unknown_tool") == TIER_CHEAP
    assert select_model_tier("execute", "unknown_tool") == TIER_STANDARD


def test_select_none_tool_uses_phase_tier():
    assert select_model_tier("review", None) == TIER_CHEAP


# ---------------------------------------------------------------------------
# get_model_for_tier — known tier + provider combos
# ---------------------------------------------------------------------------


def test_get_anthropic_cheap_returns_haiku():
    result = get_model_for_tier(TIER_CHEAP, "anthropic", catalog=_SAMPLE_CATALOG)
    assert result == "claude-haiku-4-5-20251001"


def test_get_anthropic_standard_returns_sonnet():
    result = get_model_for_tier(TIER_STANDARD, "anthropic", catalog=_SAMPLE_CATALOG)
    assert result == "claude-sonnet-4-6"


def test_get_anthropic_powerful_returns_opus():
    result = get_model_for_tier(TIER_POWERFUL, "anthropic", catalog=_SAMPLE_CATALOG)
    assert result == "claude-opus-4-8"


def test_get_bedrock_standard_returns_sonnet():
    result = get_model_for_tier(TIER_STANDARD, "bedrock", catalog=_SAMPLE_CATALOG)
    assert result == "claude-sonnet-4-6"


def test_get_bedrock_powerful_returns_sonnet_fallback():
    result = get_model_for_tier(TIER_POWERFUL, "bedrock", catalog=_SAMPLE_CATALOG)
    assert result == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# get_model_for_tier — catalog with missing preference (fallback to first pref)
# ---------------------------------------------------------------------------


def test_get_model_falls_back_to_first_pref_when_not_in_catalog():
    # Catalog only has sonnet; opus (preferred for powerful) is absent.
    limited_catalog = [
        {
            "id": "anthropic",
            "name": "Anthropic",
            "models": [{"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"}],
        }
    ]
    result = get_model_for_tier(TIER_POWERFUL, "anthropic", catalog=limited_catalog)
    # First preference (opus) is not in catalog so falls to second (sonnet)
    assert result == "claude-sonnet-4-6"


def test_get_model_returns_first_pref_when_catalog_empty():
    result = get_model_for_tier(TIER_STANDARD, "anthropic", catalog=[])
    # No catalog → return first preference without validation
    assert result == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# get_model_for_tier — edge cases
# ---------------------------------------------------------------------------


def test_get_unknown_provider_returns_none():
    result = get_model_for_tier(TIER_STANDARD, "openai", catalog=_SAMPLE_CATALOG)
    assert result is None


def test_get_unknown_provider_empty_catalog_returns_none():
    result = get_model_for_tier(TIER_STANDARD, "openai", catalog=[])
    assert result is None


def test_get_unknown_tier_returns_none():
    result = get_model_for_tier("ultra", "anthropic", catalog=_SAMPLE_CATALOG)
    assert result is None


def test_get_model_none_catalog_uses_empty_fallback():
    # When catalog=None and the in-memory cache is also empty, fall back to
    # first preference rather than crashing.
    import util.models_dev as _md

    original = _md._cached_providers
    try:
        _md._cached_providers = None
        result = get_model_for_tier(TIER_STANDARD, "anthropic", catalog=None)
        assert result == "claude-sonnet-4-6"
    finally:
        _md._cached_providers = original


def test_get_model_none_catalog_uses_in_memory_cache():
    # When catalog=None the in-memory cache from models_dev should be consulted.
    import util.models_dev as _md

    original = _md._cached_providers
    try:
        _md._cached_providers = [
            {
                "id": "anthropic",
                "name": "Anthropic",
                "models": [{"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"}],
            }
        ]
        result = get_model_for_tier(TIER_STANDARD, "anthropic", catalog=None)
        assert result == "claude-sonnet-4-6"
    finally:
        _md._cached_providers = original
