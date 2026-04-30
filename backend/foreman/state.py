"""Shared in-memory state for the foreman AI.

Kept in its own module so both runner.py and tools.py can import it
without creating a circular dependency.
"""

from typing import Dict, List

# Per-guild foreman conversation history (trimmed to ~40 messages).
foreman_conversations: Dict[str, List[dict]] = {}
