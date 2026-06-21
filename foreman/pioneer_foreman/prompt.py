"""Foreman system prompt and system-prompt builder.

All logic lives in backend.foreman_core.prompt — this module re-exports for
backward-compatibility with existing imports (from pioneer_foreman.prompt import ...).
"""

from backend.foreman_core.prompt import (
    _EMPTY_WORKERS_BLOCKS,
    CHILD_FOREMAN_SYSTEM,
    FOREMAN_SYSTEM,
    _stable_system_text,
    build_child_state_preamble,
    build_child_system_blocks,
    build_state_preamble,
    build_system_blocks,
    build_system_prompt,
)

__all__ = [
    "FOREMAN_SYSTEM",
    "CHILD_FOREMAN_SYSTEM",
    "_EMPTY_WORKERS_BLOCKS",
    "_stable_system_text",
    "build_child_state_preamble",
    "build_child_system_blocks",
    "build_state_preamble",
    "build_system_blocks",
    "build_system_prompt",
]
