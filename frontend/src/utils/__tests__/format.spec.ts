import { describe, expect, it } from 'vitest'
import { formatWorkerId } from '../format'

// These test cases mirror backend/tests/test_worker_name.py (test_format_*).
// When adding cases here, add the same case to the Python test suite and vice versa.
describe('formatWorkerId', () => {
  it.each([
    ['w-vd3566', 'VD-3566'],
    ['w-ab1234', 'AB-1234'],
    ['w-x9', 'X-9'],
    ['w-abc123', 'ABC-123'],
    ['w-g2otus', 'G-2OTUS'],
  ])('%s → %s', (input, expected) => {
    expect(formatWorkerId(input)).toBe(expected)
  })

  it('strips the w- prefix', () => {
    const result = formatWorkerId('w-vd3566')
    expect(result).not.toMatch(/^w-/i)
  })

  it('returns a fully uppercased result', () => {
    const result = formatWorkerId('w-vd3566')
    expect(result).toBe(result.toUpperCase())
  })

  // Edge cases — same as Python test_format_* edge cases
  it('all-digit suffix (w-1234) returns digits without a hyphen', () => {
    expect(formatWorkerId('w-1234')).toBe('1234')
  })

  it('no-digit suffix (w-abc) returns uppercased letters without a hyphen', () => {
    expect(formatWorkerId('w-abc')).toBe('ABC')
  })
})
