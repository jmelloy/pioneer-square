"""Tests for the analyze_epic foreman tool schema and two-tier architecture."""

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
    assert "trigger_deep_analysis" in props


def test_analyze_epic_in_child_tools():
    """analyze_epic should be available to child contexts (not excluded)."""
    from backend.foreman.tools_schema import CHILD_FOREMAN_TOOLS

    child_names = [t["name"] for t in CHILD_FOREMAN_TOOLS]
    assert "analyze_epic" in child_names


def test_analyze_epic_description_mentions_two_levels():
    """Tool description should reference the two-tier architecture."""
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    tool = next(t for t in FOREMAN_TOOLS if t["name"] == "analyze_epic")
    desc = tool["description"]
    assert "Level 1" in desc
    assert "Level 2" in desc or "deep_epic_analysis" in desc or "trigger_deep_analysis" in desc


def test_analyze_epic_file_gap_issues_default_false():
    """file_gap_issues should default to false in Level 1 (lightweight mode)."""
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    tool = next(t for t in FOREMAN_TOOLS if t["name"] == "analyze_epic")
    desc = tool["input_schema"]["properties"]["file_gap_issues"]["description"]
    assert "Default: false" in desc
