/* =========================================================================
   WHICH WAY THE MONEY MOVED
   =========================================================================
   The Decision Twin's delta badge is the loudest claim the product makes
   about a decision. It once made that claim BACKWARDS: the engine reported an
   $8,000 RRSP contribution reducing tax by $2,403.79, and the badge announced
   it — visually and to screen readers — as $2,403.79 HIGHER, in the colour
   reserved for a worse position.

   Nothing caught it. The value-parity journeys asserted the FIGURE appeared;
   none asserted what the product said the figure MEANT. These do.

   The cause was reading `tax_delta` as an ordinary signed difference. It is
   not: the engine computes `baseline_tax - scenario_tax` and labels the
   result `current_year_tax_reduction`, so a positive amount is a REDUCTION.
   ========================================================================= */
import { describe, expect, it } from 'vitest'
import { deltaTone } from './DecisionTwin'

/** The sealed money shape as the backend sends it. */
function delta(amount: string, effect_type = 'current_year_tax_reduction') {
  return {
    amount,
    effect_type,
    calculation_basis: 'scenario_estimate',
    evidence_status: 'user_attested',
    tax_year: 2025,
    currency_code: 'CAD',
    horizon_years: 0,
  } as Parameters<typeof deltaTone>[0]
}

describe('delta direction', () => {
  it('reads a POSITIVE reduction as lower tax', () => {
    // The exact case that shipped inverted.
    const tone = deltaTone(delta('2403.79'))
    expect(tone.word).toBe('Lower')
    expect(tone.arrow).toBe('↓')
    expect(tone.modifier).toContain('down')
    expect(tone.spoken).toMatch(/lower/)
    expect(tone.spoken).not.toMatch(/higher/)
  })

  it('reads a NEGATIVE reduction as higher tax', () => {
    const tone = deltaTone(delta('-880.00'))
    expect(tone.word).toBe('Higher')
    expect(tone.arrow).toBe('↑')
    expect(tone.modifier).toContain('up')
    expect(tone.spoken).toMatch(/higher/)
  })

  it('never calls a difference of nothing a movement', () => {
    const tone = deltaTone(delta('0.00'))
    expect(tone.word).toBe('No change')
    expect(tone.modifier).toBe('')
  })

  it('states the magnitude, never a re-signed figure', () => {
    // The badge shows how much it moved; the WORD carries the direction. A
    // minus sign beside the word "Higher" would say it twice and contradict
    // itself once.
    expect(deltaTone(delta('-880.00')).spoken).toContain('$880.00')
    expect(deltaTone(delta('-880.00')).spoken).not.toContain('-$')
  })

  it('makes NO directional claim about an effect type it does not know', () => {
    // The failure mode being closed: inferring a direction from a sign whose
    // meaning has not been established. An unrecognised effect type gets its
    // own name and no arrow, rather than a confident guess.
    const tone = deltaTone(delta('1200.00', 'liquidity_commitment'))
    expect(tone.word).not.toBe('Lower')
    expect(tone.word).not.toBe('Higher')
    expect(tone.modifier).toBe('')
    expect(tone.word.toLowerCase()).toContain('liquidity')
  })

  it('does not invent a direction from an unparseable amount', () => {
    const tone = deltaTone(delta('not-a-number'))
    expect(tone.word).not.toBe('Lower')
    expect(tone.word).not.toBe('Higher')
  })
})
