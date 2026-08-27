"""Regenerate frontend/src/generated/ws-protocol.json from ws_types.py.

Run this after changing any WS message model in backend/ws_types.py — CI
enforces freshness via backend/tests/test_ws_protocol_parity.py, and
frontend/src/generated/ws-protocol.spec.ts diffs the frontend's own WS type
declarations against this file so the two protocols can't silently drift.

    python scripts/export_ws_protocol.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_ROOT))

from ws_types import protocol_spec  # noqa: E402

_OUT_PATH = _BACKEND_ROOT.parent / "frontend" / "src" / "generated" / "ws-protocol.json"


def main() -> None:
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(protocol_spec(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {_OUT_PATH}")


if __name__ == "__main__":
    main()
