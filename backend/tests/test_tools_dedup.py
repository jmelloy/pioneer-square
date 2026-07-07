"""Verify the backend Foreman tool schema."""

import os
import sys

# Repo root — needed so `import backend` resolves backend.foreman.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)


def test_foreman_tools_canonical_is_list():
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    assert isinstance(FOREMAN_TOOLS, list)
    assert len(FOREMAN_TOOLS) > 0
    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "create_task" in names
    assert "assign_task" in names


def test_spawn_worker_not_in_foreman_tools():
    """spawn_worker must stay out of FOREMAN_TOOLS until async/lock fixes land (#567)."""
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "spawn_worker" not in names, (
        "spawn_worker must not be exposed to the foreman AI until issues #551, #564, #566 "
        "are merged and tested (see #567)"
    )
