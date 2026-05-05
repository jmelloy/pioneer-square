"""Unit tests for Agent auto-generation of id and name."""

from __future__ import annotations

import re

import pytest
from pioneer_worker.worker import Agent


def test_default_id_is_auto_generated():
    """When no id is supplied, Agent must generate a non-None string id."""
    slot = Agent()
    assert slot.id is not None
    assert isinstance(slot.id, str)
    assert len(slot.id) > 0


def test_default_id_has_agent_prefix():
    """Auto-generated ids should carry the 'a-' prefix from _gen_id."""
    slot = Agent()
    assert slot.id.startswith("a-")


def test_agent_id_property_matches_id():
    """agent_id property must return the same value as id for back-compat."""
    slot = Agent()
    assert slot.agent_id == slot.id


def test_explicit_id_is_preserved():
    """An explicitly provided id must not be overwritten."""
    slot = Agent(id="a-custom")
    assert slot.id == "a-custom"
    assert slot.agent_id == "a-custom"


def test_default_name_is_auto_generated():
    """When no name is supplied, Agent must generate a non-None string name."""
    slot = Agent()
    assert slot.name is not None
    assert isinstance(slot.name, str)
    assert len(slot.name) > 0


def test_default_name_is_droid_style():
    """Auto-generated name must look like a droid designation (XX-YY uppercase)."""
    slot = Agent()
    assert re.match(r"^[A-Z0-9]+-[A-Z0-9]+$", slot.name), f"Unexpected name: {slot.name!r}"


def test_explicit_name_is_preserved():
    """An explicitly provided name must not be overwritten."""
    slot = Agent(name="mybot/1")
    assert slot.name == "mybot/1"


def test_two_default_slots_get_distinct_ids():
    """Each slot must get a distinct auto-generated id."""
    slot_a = Agent()
    slot_b = Agent()
    assert slot_a.id != slot_b.id


def test_id_and_name_both_explicit():
    """Both id and name can be set explicitly at the same time."""
    slot = Agent(id="a-xyz123", name="R2-D2")
    assert slot.id == "a-xyz123"
    assert slot.name == "R2-D2"
    assert slot.agent_id == "a-xyz123"


def test_default_state_is_idle():
    """Slot must start in idle state regardless of auto-generation."""
    slot = Agent()
    assert slot.state == "idle"
    assert slot.current_task_id is None
    assert slot.activity is None


def test_droid_name_is_deterministic_for_same_id():
    """The same id must always produce the same droid name."""
    slot_a = Agent(id="a-abc123")
    slot_b = Agent(id="a-abc123")
    assert slot_a.name == slot_b.name


def test_droid_name_differs_for_different_ids():
    """Two distinct ids must produce different droid names (with overwhelming probability)."""
    slot_a = Agent(id="a-abc123")
    slot_b = Agent(id="a-xyz999")
    assert slot_a.name != slot_b.name
