"""Build the worker's tool-runner registry."""

from __future__ import annotations

from . import claude_runner, codex_runner, config, pi_runner
from .runner_types import Runner  # pyright: ignore[reportMissingImports]


def build(cfg: config.Config) -> dict[str, Runner]:
    """Return configured runners keyed by worker tool name."""
    return {
        "claude": claude_runner.ClaudeRunner(
            claude_path=cfg.claude_path,
            max_turns=cfg.claude_max_turns,
        ),
        "codex": codex_runner.CodexRunner(
            codex_path=cfg.codex_path,
            codex_args=cfg.codex_args,
            openai_api_key=cfg.openai_api_key,
        ),
        "pi": pi_runner.PiRunner(
            pi_path=cfg.pi_path,
            model=cfg.pi_model,
            provider=cfg.pi_provider,
        ),
    }
