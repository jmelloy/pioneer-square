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


def test_analyze_epic_description_mentions_two_levels():
    """Tool description should reference the two-tier architecture."""
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    tool = next(t for t in FOREMAN_TOOLS if t["name"] == "analyze_epic")
    desc = tool["description"]
    assert "Level 1" in desc
    assert "Level 2" in desc or "deep_epic_analysis" in desc or "trigger_deep_analysis" in desc


def test_analyze_epic_file_gap_issues_is_deprecated_noop():
    """file_gap_issues no longer files issues; the schema must say so.

    Gap detection uses naive substring matching against PR titles/bodies and
    is prone to false positives, so it must not drive automatic issue
    creation. The parameter is kept for backward compatibility only.
    """
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    tool = next(t for t in FOREMAN_TOOLS if t["name"] == "analyze_epic")
    desc = tool["input_schema"]["properties"]["file_gap_issues"]["description"]
    assert "Deprecated" in desc
    assert "no longer creates issues" in desc


async def test_analyze_epic_dispatches(monkeypatch):
    """Regression: analyze_epic must be in the GitHub-tool gate so its handler is
    reachable. A dead branch would leave result_text="" (empty content); a reachable
    branch with no token returns the 'No GitHub token' error."""
    import auth_deps
    import foreman.tools as tools

    class _FakeDB:
        async def close(self):
            pass

    async def _fake_get_db():
        return _FakeDB()

    async def _fake_guild_pk(db, guild_id):
        return 0

    async def _fake_token(guild_id, user_id=None):
        return None  # no creds -> handler returns the token error, proving dispatch

    monkeypatch.setattr(tools, "get_db", _fake_get_db)
    monkeypatch.setattr(auth_deps, "get_guild_pk", _fake_guild_pk)
    monkeypatch.setattr(tools, "_guild_github_token", _fake_token)

    class _ToolUse:
        id = "tool-1"
        name = "analyze_epic"
        input = {"repo": "o/r", "issue_number": 1}

    block = await tools._exec_one_tool("g1", _ToolUse())

    assert block["content"], "analyze_epic returned empty content — handler unreachable"
    assert block.get("is_error") is True
    assert "No GitHub token" in block["content"]
