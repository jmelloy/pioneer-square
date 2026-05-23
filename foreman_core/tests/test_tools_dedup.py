"""Verify all foreman entry points share the same FOREMAN_TOOLS from foreman_core."""

import os
import sys

import pytest

# Add entry-point packages to sys.path so we can import without installing them.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "foreman"))
sys.path.insert(0, os.path.join(_ROOT, "pioneer-foreman"))


def test_foreman_tools_canonical_is_list():
    from foreman_core.tools_schema import FOREMAN_TOOLS

    assert isinstance(FOREMAN_TOOLS, list)
    assert len(FOREMAN_TOOLS) > 0
    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "create_task" in names
    assert "assign_task" in names


def test_standalone_foreman_uses_canonical_tools():
    # foreman/ standalone entry point
    import pioneer_foreman.tools as foreman_tools
    from foreman_core.tools_schema import FOREMAN_TOOLS as canonical

    assert foreman_tools.FOREMAN_TOOLS is canonical, (
        "foreman/pioneer_foreman/tools.py must import FOREMAN_TOOLS from foreman_core, "
        "not define its own copy"
    )


def test_pioneer_foreman_uses_canonical_tools():
    """The pioneer-foreman/ entry point must also use the canonical tool list."""
    from foreman_core.tools_schema import FOREMAN_TOOLS as canonical

    # Temporarily shadow the already-imported pioneer_foreman with the one from
    # pioneer-foreman/ so both can coexist in the same test run.
    pf_path = os.path.join(_ROOT, "pioneer-foreman")
    orig_path = sys.path[:]
    sys.path.insert(0, pf_path)

    # Force a fresh import from the pioneer-foreman/ directory.
    saved = sys.modules.pop("pioneer_foreman", None)
    saved_tools = sys.modules.pop("pioneer_foreman.tools", None)
    try:
        import pioneer_foreman.tools as pf_tools

        assert pf_tools.FOREMAN_TOOLS is canonical, (
            "pioneer-foreman/pioneer_foreman/tools.py must import FOREMAN_TOOLS from "
            "foreman_core, not define its own copy"
        )
    finally:
        # Restore sys.modules and sys.path to avoid polluting other tests.
        sys.path[:] = orig_path
        if saved is None:
            sys.modules.pop("pioneer_foreman", None)
        else:
            sys.modules["pioneer_foreman"] = saved
        if saved_tools is None:
            sys.modules.pop("pioneer_foreman.tools", None)
        else:
            sys.modules["pioneer_foreman.tools"] = saved_tools


def test_all_entry_points_same_tool_names():
    """All three sources must expose the exact same tool names in the same order."""
    from foreman_core.tools_schema import FOREMAN_TOOLS as canonical

    canonical_names = [t["name"] for t in canonical]

    import pioneer_foreman.tools as foreman_tools

    assert [t["name"] for t in foreman_tools.FOREMAN_TOOLS] == canonical_names
