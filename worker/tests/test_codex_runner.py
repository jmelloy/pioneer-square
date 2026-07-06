from __future__ import annotations

import os
import sys

import pytest
from pioneer_worker.codex_runner import parse_codex_event, run_codex_auto


def test_parse_codex_assistant_message() -> None:
    assert parse_codex_event({"type": "message", "role": "assistant", "content": "done"}) == (
        "done",
        None,
    )


def test_parse_codex_function_result_preserves_full_output_in_detail() -> None:
    """The display line is truncated to 200 chars, but detail carries the full output (#781)."""
    long_output = "x" * 500
    text, detail = parse_codex_event({"type": "function_result", "output": long_output})
    assert text == f"  → {long_output[:200]}"
    assert detail == {"toolType": "tool_result", "output": long_output}


def test_parse_codex_function_call_preserves_full_arguments_in_detail() -> None:
    long_args = "y" * 500
    text, detail = parse_codex_event({"type": "function_call", "name": "shell", "arguments": long_args})
    assert text == f"▶ shell({long_args[:80]})"
    assert detail == {"toolType": "tool_use", "name": "shell", "input": long_args}


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

print(json.dumps({{"type": "message", "role": "assistant", "content": "finished"}}), flush=True)
print(json.dumps({{"type": "done"}}), flush=True)
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
