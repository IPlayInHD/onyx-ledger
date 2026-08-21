import { describe, expect, it } from 'vitest'
import {
  humanize,
  isDecrease,
  isoDate,
  magnitude,
  money,
  percent,
  plainNumber,
  relativeDays,
} from './format'

describe('money', () => {
  it('renders a canonical backend string at cent precision', () => {
    expect(money('9348.85')).toBe('$9,348.85')
    expect(money('60000.00')).toBe('$60,000.00')
  })

  it('preserves the sign the backend sent rather than reinterpreting it', () => {
    expect(money('-1483.25')).toBe('-$1,483.25')
  })

  it('shows an em dash for an absent value instead of inventing a zero', () => {
    // Rendering "$0.00" for a missing figure would be a false statement about
    // someone's tax position, not a formatting nicety.
    expect(money(null)).toBe('—')
    expect(money(undefined)).toBe('—')
    expect(money('')).toBe('—')
  })

  it('passes through a value it cannot parse rather than dropping it', () => {
    expect(money('not-a-number')).toBe('not-a-number')
  })

  it('does not round away cents', () => {
    expect(money('1234.56')).toContain('.56')
    expect(money('0.01')).toBe('$0.01')
  })
})

describe('magnitude and direction', () => {
  it('reports magnitude without a sign so direction can be shown in words', () => {
    expect(magnitude('-1483.25')).toBe('$1,483.25')
    expect(magnitude('1483.25')).toBe('$1,483.25')
  })

  it('detects a decrease only from a genuinely negative value', () => {
    expect(isDecrease('-1483.25')).toBe(true)
    expect(isDecrease('1483.25')).toBe(false)
    expect(isDecrease(null)).toBe(false)
    expect(isDecrease('abc')).toBe(false)
  })
})

describe('percent', () => {
  it('renders a backend fraction as a percentage', () => {
    expect(percent('0.2965')).toBe('29.65%')
    expect(percent('0.15')).toBe('15%')
  })

  it('shows an em dash rather than 0% when there is no rate', () => {
    expect(percent(null)).toBe('—')
  })
})

describe('isoDate', () => {
  it('renders an ISO date readably', () => {
    expect(isoDate('2026-04-30')).toBe('April 30, 2026')
  })

  it('returns the original string when it will not parse', () => {
    expect(isoDate('not-a-date')).toBe('not-a-date')
  })

  it('shows an em dash for no date rather than today', () => {
    expect(isoDate(null)).toBe('—')
  })
})

describe('humanize', () => {
  it('turns a machine code into readable words without altering the code', () => {
    expect(humanize('RESOURCE_EXHAUSTED')).toBe('Resource exhausted')
    expect(humanize('CONTRIBUTION_ROOM_AVAILABLE')).toBe('Contribution room available')
  })

  it('is safe on empty input', () => {
    expect(humanize(null)).toBe('')
    expect(humanize('')).toBe('')
  })
})

describe('plainNumber and relativeDays', () => {
  it('groups plain counts', () => {
    expect(plainNumber(1234)).toBe('1,234')
  })

  it('describes day counts in words', () => {
    expect(relativeDays(0)).toBe('today')
    expect(relativeDays(1)).toBe('tomorrow')
    expect(relativeDays(45)).toBe('in 45 days')
    expect(relativeDays(-3)).toBe('3 days ago')
  })
})
