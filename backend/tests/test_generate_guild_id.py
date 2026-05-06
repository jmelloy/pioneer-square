"""Unit tests for the generate_guild_id() helper."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import _candidate_from_name, generate_guild_id

# ---------------------------------------------------------------------------
# _candidate_from_name unit tests
# ---------------------------------------------------------------------------


def test_pioneer_square():
    # "Pioneer Square" → slug "pioneer-square" → devowel "pnr-sqr" (7 chars, ≤8, ≥5)
    assert _candidate_from_name("Pioneer Square") == "pnr-sqr"


def test_all_consonants_already_short():
    # "Sqr" → slug "sqr" → devowel "sqr" (3 chars < 5) → fall back to slug "sqr"
    assert _candidate_from_name("Sqr") == "sqr"


def test_name_with_special_chars():
    # Non-alphanumeric becomes hyphens; accented chars are stripped via NFKD
    result = _candidate_from_name("Héllo Wörld!")
    # "héllo wörld!" → ascii normalize → "hello world!" → slug "hello-world"
    # devowel "hll-wrld" (8 chars exactly, within target)
    assert result == "hll-wrld"


def test_name_with_only_vowels():
    # "aeiou" → slug "aeiou" → devowel "" → too short → fall back to slug "aeiou"
    assert _candidate_from_name("aeiou") == "aeiou"


def test_long_name_trimmed_to_target():
    # A slug longer than 8 chars after devoweling should be trimmed to ≤8 without going below 5.
    result = _candidate_from_name("Steampunk Factory Floor")
    # slug: "steampunk-factory-floor"
    # devowel: "stmpnk-fctr-flr" (15 chars) → trim from end to 8
    assert len(result) <= 8
    assert len(result) >= 5


def test_empty_name_falls_back_to_random():
    result = generate_guild_id(name="", existing_ids=set())
    assert len(result) == 6
    assert result.isalnum()


def test_no_name_falls_back_to_random():
    result = generate_guild_id()
    assert len(result) == 6
    assert result.isalnum()


# ---------------------------------------------------------------------------
# generate_guild_id integration tests
# ---------------------------------------------------------------------------


def test_normal_case_unique_on_first_try():
    result = generate_guild_id(name="Pioneer Square", existing_ids=set())
    assert result == "pnr-sqr"


def test_collision_appends_random_suffix():
    existing = {"pnr-sqr"}
    result = generate_guild_id(name="Pioneer Square", existing_ids=existing)
    assert result.startswith("pnr-sqr-")
    suffix = result[len("pnr-sqr-") :]
    assert len(suffix) == 4
    assert result not in existing


def test_collision_suffix_also_collides():
    # Force a collision on the first suffix by pre-populating many suffixes.
    import random as _random

    _random.seed(42)
    first_suffix = "".join(_random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
    _random.seed(42)
    existing = {"pnr-sqr", f"pnr-sqr-{first_suffix}"}
    result = generate_guild_id(name="Pioneer Square", existing_ids=existing)
    assert result.startswith("pnr-sqr-")
    assert result not in existing


def test_short_name_below_min():
    # "AI" → slug "ai" → devowel "" → too short → fall back to slug "ai" (2 chars < 5)
    # The candidate is "ai"; it's returned as-is since it doesn't collide.
    result = generate_guild_id(name="AI", existing_ids=set())
    assert result == "ai"


def test_short_name_collision_gets_suffix():
    result = generate_guild_id(name="AI", existing_ids={"ai"})
    assert result.startswith("ai-")
    assert result not in {"ai"}


def test_special_characters_in_name():
    # Unicode + punctuation should produce a valid slug
    result = generate_guild_id(name="Héllo Wörld! @2024", existing_ids=set())
    assert result == "hll-wrld"


def test_name_with_consecutive_special_chars():
    result = generate_guild_id(name="foo  ---  bar", existing_ids=set())
    # slug: "foo-bar" → devowel "f-br" (4 chars < 5) → fall back to "foo-bar"
    assert result == "foo-bar"


def test_unique_id_not_in_existing():
    existing = {"pnr-sqr"}
    result = generate_guild_id(name="Pioneer Square", existing_ids=existing)
    assert result not in existing


def test_result_is_deterministic_without_collision():
    # Same name + empty existing always gives the same result.
    r1 = generate_guild_id(name="My Workspace", existing_ids=set())
    r2 = generate_guild_id(name="My Workspace", existing_ids=set())
    assert r1 == r2
