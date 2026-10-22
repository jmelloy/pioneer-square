from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from unittest.mock import AsyncMock

import pytest
from pioneer_worker.codex_runner import CodexRunner, parse_codex_event, run_codex_auto


def test_parse_codex_assistant_message() -> None:
    assert parse_codex_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
    ) == ("done", None)
    # item.started for a message carries no final text yet — nothing to display.
    assert parse_codex_event(
        {"type": "item.started", "item": {"type": "agent_message", "text": ""}}
    ) == (None, None)


def test_parse_codex_turn_completed_marks_done() -> None:
    assert parse_codex_event({"type": "turn.completed", "usage": {}}) == ("✓ Done", None)


def test_parse_codex_command_result_preserves_full_output_in_detail() -> None:
    """The display line is truncated to 200 chars, but detail carries the full output (#781)."""
    long_output = "x" * 500
    text, detail = parse_codex_event(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "echo hi",
                "aggregated_output": long_output,
                "exit_code": 0,
            },
        }
    )
    assert text == f"  → (exit 0) {long_output[:200]}"
    assert detail == {"toolType": "tool_result", "output": long_output}


def test_parse_codex_command_start_preserves_command_in_detail() -> None:
    long_cmd = "echo " + "y" * 500
    text, detail = parse_codex_event(
        {"type": "item.started", "item": {"type": "command_execution", "command": long_cmd}}
    )
    assert text == f"▶ {long_cmd[:100]}"
    assert detail == {"toolType": "tool_use", "name": "shell", "input": long_cmd}


@pytest.mark.asyncio
async def test_run_codex_auto_uses_tty_stdin(tmp_path) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

if not os.isatty(0):
    print("Reading additional input from stdin...", file=sys.stderr, flush=True)
    sys.stdin.read()
    sys.exit(2)

assert "-C" in sys.argv
assert sys.argv[sys.argv.index("-C") + 1] == os.getcwd()
assert "--sandbox" in sys.argv
assert sys.argv[sys.argv.index("--sandbox") + 1] == "workspace-write"
last_message_path = sys.argv[sys.argv.index("--output-last-message") + 1]
with open(last_message_path, "w", encoding="utf-8") as fh:
    fh.write("captured final")

print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "finished"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{}}}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    emitted: list[str] = []

    async def emit(line: str, detail: dict | None = None) -> None:
        emitted.append(line)

    success, stop_reason, last_text, session_id = await run_codex_auto(
        "do the work",
        str(tmp_path),
        emit=emit,
        codex_path=os.fspath(fake_codex),
        codex_args=["--sandbox", "workspace-write"],
    )

    assert success is True
    assert stop_reason == "success"
    assert last_text == "captured final"
    assert session_id is None
    assert "finished" in emitted
    assert not any("Reading additional input from stdin" in line for line in emitted)


@pytest.fixture
def capturing_codex(tmp_path):
    capture = tmp_path / "capture.json"
    executable = tmp_path / "capture-codex"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path
Path({str(capture)!r}).write_text(json.dumps({{
    "argv": sys.argv[1:],
    "key": os.environ.get("CODEX_API_KEY"),
}}))
print(json.dumps({{"type": "turn.completed"}}))
"""
    )
    executable.chmod(0o755)
    return str(executable), capture


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_session_id", [None, "existing-session"])
async def test_codex_defaults_allow_network_and_linked_git_metadata(
    tmp_path, capturing_codex, resume_session_id
):
    clone = tmp_path / 'clone with "quotes"'
    worktree = tmp_path / "task"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "worktree", "add", "-qb", "task", str(worktree)],
        check=True,
    )
    executable, capture = capturing_codex
    success, *_ = await run_codex_auto(
        "work",
        str(worktree),
        emit=AsyncMock(),
        codex_path=executable,
        resume_session_id=resume_session_id,
        codex_args=[],
    )
    assert success
    argv = json.loads(capture.read_text())["argv"]
    overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    policy = tomllib.loads("\n".join(overrides))
    assert policy["approval_policy"] == "never"
    assert policy["default_permissions"] == "pioneer"
    profile = policy["permissions"]["pioneer"]
    assert profile["extends"] == ":workspace"
    assert profile["network"]["enabled"] is True
    assert set(profile["filesystem"]) == {
        str((clone / ".git").resolve()),
        str((clone / ".git" / "worktrees" / "task").resolve()),
    }
    assert set(profile["filesystem"].values()) == {"write"}
    if resume_session_id:
        assert argv[argv.index("resume") + 1] == resume_session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "configured_key", "expected"),
    [
        ({"OPENAI_API_KEY": "legacy-key"}, None, "legacy-key"),
        ({}, "configured-key", "configured-key"),
        ({"CODEX_API_KEY": "scoped-key", "OPENAI_API_KEY": "other-key"}, None, "scoped-key"),
        ({"CODEX_API_KEY": "scoped-key"}, "configured-key", "scoped-key"),
        ({}, None, None),
    ],
)
async def test_codex_invocation_api_key(tmp_path, capturing_codex, env, configured_key, expected):
    executable, capture = capturing_codex
    success, *_ = await run_codex_auto(
        "work",
        str(tmp_path),
        emit=AsyncMock(),
        codex_path=executable,
        codex_args=["--sandbox", "read-only"],
        env=env,
        openai_api_key=configured_key,
    )
    assert success
    captured = json.loads(capture.read_text())
    assert captured["key"] == expected
    assert "default_permissions" not in " ".join(captured["argv"])


@pytest.mark.asyncio
async def test_codex_native_api_key_passes_credential_probe():
    assert await CodexRunner(codex_path="does-not-exist").probe_credentials(
        {"CODEX_API_KEY": "scoped-key"}
    )
