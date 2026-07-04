"""Verify all foreman entry points share the same FOREMAN_TOOLS from backend.foreman_core."""

import os
import sys

# Repo root — needed so `import backend` resolves backend/foreman_core.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# Add the standalone foreman package to sys.path so we can import without installing it.
sys.path.insert(0, os.path.join(_ROOT, "foreman"))


def test_foreman_tools_canonical_is_list():
    from backend.foreman_core.tools_schema import FOREMAN_TOOLS

    assert isinstance(FOREMAN_TOOLS, list)
    assert len(FOREMAN_TOOLS) > 0
    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "create_task" in names
    assert "assign_task" in names


def test_spawn_worker_in_foreman_tools():
    """spawn_worker is exposed now that #551, #564, #566 have landed (see #567, #725)."""
    from backend.foreman_core.tools_schema import FOREMAN_TOOLS

    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "spawn_worker" in names


def test_standalone_foreman_uses_canonical_tools():
    # foreman/ standalone entry point
    import pioneer_foreman.tools as foreman_tools

    from backend.foreman_core.tools_schema import FOREMAN_TOOLS as canonical

    assert foreman_tools.FOREMAN_TOOLS is canonical, (
        "foreman/pioneer_foreman/tools.py must import FOREMAN_TOOLS from backend.foreman_core, "
        "not define its own copy"
    )


def test_all_entry_points_same_tool_names():
    """All entry points must expose the exact same tool names in the same order."""
    from backend.foreman_core.tools_schema import FOREMAN_TOOLS as canonical

    canonical_names = [t["name"] for t in canonical]

    import pioneer_foreman.tools as foreman_tools

    assert [t["name"] for t in foreman_tools.FOREMAN_TOOLS] == canonical_names
