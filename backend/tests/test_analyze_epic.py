"""Tests for the analyze_epic foreman tool schema and safeguard logic."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)


def test_analyze_epic_in_foreman_tools():
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "analyze_epic" in names


def test_analyze_epic_schema_has_required_fields():
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    tool = next(t for t in FOREMAN_TOOLS if t["name"] == "analyze_epic")
    schema = tool["input_schema"]
    assert schema["required"] == ["repo", "issue_number"]
    props = schema["properties"]
    assert "repo" in props
    assert "issue_number" in props
    assert "force" in props
    assert "file_gap_issues" in props


def test_analyze_epic_in_child_tools():
    """analyze_epic should be available to child contexts (not excluded)."""
    from backend.foreman.tools_schema import CHILD_FOREMAN_TOOLS

    child_names = [t["name"] for t in CHILD_FOREMAN_TOOLS]
    assert "analyze_epic" in child_names
