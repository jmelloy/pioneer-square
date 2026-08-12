"""Tests for the interactive agent run request schema.

Execution is intentionally task-backed now; backend/agent_runner.py only owns the
HTTP request model and contains no subprocess runner.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_runner import RunAgentRequest  # noqa: E402


def test_run_agent_request_accepts_pi_options():
    req = RunAgentRequest(
        tool="pi",
        prompt="help me inspect this repo",
        model="anthropic/claude-sonnet-4-5",
        provider="bedrock",
    )
    assert req.tool == "pi"
    assert req.prompt == "help me inspect this repo"
    assert req.model == "anthropic/claude-sonnet-4-5"
    assert req.provider == "bedrock"
