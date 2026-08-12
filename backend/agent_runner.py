"""Request schema for interactive agent runs.

The historical backend-owned subprocess runner was removed: the Run button now
creates a task and lets the worker package own CLI execution, parsing, logs, and
message injection just like regular tasks.
"""

from __future__ import annotations

from pydantic import BaseModel


class RunAgentRequest(BaseModel):
    tool: str  # "claude" | "codex" | "pi"
    prompt: str
    model: str | None = None
    provider: str | None = None
