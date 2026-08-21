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


def test_spawn_worker_in_foreman_tools():
    from backend.foreman.tools_schema import FOREMAN_TOOLS

    names = [t["name"] for t in FOREMAN_TOOLS]
    assert "spawn_worker" in names
