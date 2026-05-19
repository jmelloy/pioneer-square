from __future__ import annotations

import os
import sys

import pytest
from pioneer_worker.codex_runner import parse_codex_event, run_codex_auto


def test_parse_codex_assistant_message() -> None:
    assert parse_codex_event({"type": "message", "role": "assistant", "content": "done"}) == "done"


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

    async def emit(line: str) -> None:
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
