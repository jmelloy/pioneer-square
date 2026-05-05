"""Unit tests for _AgentSlot auto-generation of id and name."""

from __future__ import annotations

import re

import pytest
from pioneer_worker.worker import _AgentSlot


def test_default_id_is_auto_generated():
    """When no id is supplied, _AgentSlot must generate a non-None string id."""
    slot = _AgentSlot()
    assert slot.id is not None
    assert isinstance(slot.id, str)
    assert len(slot.id) > 0


def test_default_id_has_agent_prefix():
    """Auto-generated ids should carry the 'a-' prefix from _gen_id."""
    slot = _AgentSlot()
    assert slot.id.startswith("a-")


def test_agent_id_property_matches_id():
    """agent_id property must return the same value as id for back-compat."""
    slot = _AgentSlot()
    assert slot.agent_id == slot.id


def test_explicit_id_is_preserved():
    """An explicitly provided id must not be overwritten."""
    slot = _AgentSlot(id="a-custom")
    assert slot.id == "a-custom"
    assert slot.agent_id == "a-custom"


def test_default_name_is_auto_generated():
    """When no name is supplied, _AgentSlot must generate a non-None string name."""
    slot = _AgentSlot()
    assert slot.name is not None
    assert isinstance(slot.name, str)
    assert len(slot.name) > 0


def test_default_name_is_droid_style():
    """Auto-generated name must look like a droid designation (XX-YY uppercase)."""
    slot = _AgentSlot()
    assert re.match(r"^[A-Z0-9]+-[A-Z0-9]+$", slot.name), f"Unexpected name: {slot.name!r}"


def test_explicit_name_is_preserved():
    """An explicitly provided name must not be overwritten."""
    slot = _AgentSlot(name="mybot/1")
    assert slot.name == "mybot/1"


def test_two_default_slots_get_distinct_ids():
    """Each slot must get a distinct auto-generated id."""
    slot_a = _AgentSlot()
    slot_b = _AgentSlot()
    assert slot_a.id != slot_b.id


def test_id_and_name_both_explicit():
    """Both id and name can be set explicitly at the same time."""
    slot = _AgentSlot(id="a-xyz123", name="R2-D2")
    assert slot.id == "a-xyz123"
    assert slot.name == "R2-D2"
    assert slot.agent_id == "a-xyz123"


def test_default_state_is_idle():
    """Slot must start in idle state regardless of auto-generation."""
    slot = _AgentSlot()
    assert slot.state == "idle"
    assert slot.current_task_id is None
    assert slot.activity is None
