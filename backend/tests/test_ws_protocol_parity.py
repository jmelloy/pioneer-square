"""Guards against the WS protocol drifting from the frontend's own model of it.

Two invariants:

- ``KNOWN_INBOUND_TYPES`` (derived from ``InboundWSMessage``) must exactly
  match the set of types ``ws_handlers.dispatch`` actually routes — otherwise
  either a validated type has no handler, or a handler exists whose type
  isn't validated (the ``task-rejected`` bug this issue closes).
- The generated snapshot the frontend parity test
  (frontend/src/generated/ws-protocol.spec.ts) reads must be up to date with
  ws_types.py, or that test would be comparing against stale data.
"""

from __future__ import annotations

import json
from pathlib import Path

import ws_handlers
from ws_types import KNOWN_INBOUND_TYPES, protocol_spec

_GENERATED_PATH = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "generated" / "ws-protocol.json"
)


def test_known_inbound_types_match_dispatch_handlers():
    assert set(ws_handlers.HANDLERS) == KNOWN_INBOUND_TYPES


def test_generated_ws_protocol_snapshot_is_up_to_date():
    on_disk = json.loads(_GENERATED_PATH.read_text())
    assert on_disk == protocol_spec(), (
        "frontend/src/generated/ws-protocol.json is stale — run "
        "`python scripts/export_ws_protocol.py` from the repo root and commit the result."
    )
