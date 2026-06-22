"""Foreman system prompt and system-prompt builder.

All logic lives in backend.foreman_core.prompt (at backend/foreman_core/prompt.py) —
this module re-exports for backward-compatibility with existing imports
(from foreman.prompt import ...).
"""

from foreman_core.prompt import (
    _EMPTY_WORKERS_BLOCKS,
    FOREMAN_SYSTEM,
    _stable_system_text,
    build_state_preamble,
    build_system_blocks,
    build_system_prompt,
)

__all__ = [
    "FOREMAN_SYSTEM",
    "_EMPTY_WORKERS_BLOCKS",
    "_stable_system_text",
    "build_state_preamble",
    "build_system_blocks",
    "build_system_prompt",
]
