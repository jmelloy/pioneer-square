import { describe, expect, it } from 'vitest'
import protocolSpec from './ws-protocol.json'
import { WS_INBOUND_FIELDS } from '../types'

// Diffs WS_INBOUND_FIELDS (types.ts) against a snapshot generated from
// backend/ws_types.py (see scripts/export_ws_protocol.py). The frontend
// receives what the backend sends *outbound*, so that's the side we compare
// against. Regenerate the snapshot with:
//
//   python scripts/export_ws_protocol.py
//
// A backend test (test_ws_protocol_parity.py) fails CI if the snapshot
// itself goes stale, so this test always compares against the real protocol.
describe('WS protocol parity (frontend vs backend)', () => {
  const backendOutbound = protocolSpec.outbound as Record<string, string[]>

  it('every frontend-declared field is a real backend field (no phantoms)', () => {
    const phantoms: string[] = []
    for (const [type, fields] of Object.entries(WS_INBOUND_FIELDS)) {
      const backendFields = backendOutbound[type]
      // Types with no closed backend shape (extra="allow", e.g. claude-usage)
      // aren't in the snapshot at all — nothing to compare against.
      if (!backendFields) continue
      const backendSet = new Set(backendFields)
      for (const field of fields) {
        if (!backendSet.has(field)) phantoms.push(`${type}.${field}`)
      }
    }
    expect(phantoms).toEqual([])
  })
})
