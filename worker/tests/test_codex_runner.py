from __future__ import annotations

import os
import sys

import pytest
from pioneer_worker.codex_runner import parse_codex_event, run_codex_auto


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

    success, stop_reason, last_text = await run_codex_auto(
        "do the work",
        str(tmp_path),
        emit=emit,
        codex_path=os.fspath(fake_codex),
        codex_args=["--sandbox", "workspace-write"],
    )

    assert success is True
    assert stop_reason == "success"
    assert last_text == "captured final"
    assert "finished" in emitted
    assert not any("Reading additional input from stdin" in line for line in emitted)
