/* =========================================================================
   WHAT THE LEXICON PROMISES A CONSUMER SURFACE
   =========================================================================
   `lexicon.contract.test.ts` proves the mapping is COMPLETE against the
   engine. This file proves it BEHAVES: that an unknown code cannot become
   prose, that formal names keep their capitals, and that Level 1 stays short
   while Level 2 stays factual.
   ========================================================================= */
import { describe as suite, expect, it } from 'vitest'
import {
  ACTION_STATUS,
  ASSURANCE_FAMILY,
  ASSURANCE_STATUS,
  EVIDENCE_READINESS,
  LEXICON,
  OPPORTUNITY_CODE,
  REASON_CODE,
  RESOURCE_CODE,
  URGENCY_STATUS,
  describe as describeCode,
  level1,
  level2,
  technical,
  type ConsumerTerm,
  type Vocabulary,
} from './lexicon'

const VOCABULARIES = Object.keys(LEXICON) as Vocabulary[]

function everyTerm(): [Vocabulary, string, ConsumerTerm][] {
  const out: [Vocabulary, string, ConsumerTerm][] = []
  for (const vocabulary of VOCABULARIES) {
    for (const [code, term] of Object.entries(LEXICON[vocabulary])) {
      out.push([vocabulary, code, term as ConsumerTerm])
    }
  }
  return out
}

suite('unknown states cannot become prose', () => {
  it('returns null for a code nobody has written words for', () => {
    /* The exact shape of the defect this entry removes: before the lexicon,
       an unrecognised code was lowercased and de-underscored into a sentence
       that read like a reviewed translation and was not one. */
    expect(describeCode('action', 'SOME_STATE_ADDED_NEXT_YEAR')).toBeNull()
    expect(level1('action', 'SOME_STATE_ADDED_NEXT_YEAR')).toBeNull()
    expect(level2('action', 'SOME_STATE_ADDED_NEXT_YEAR')).toBeNull()
    expect(technical('action', 'SOME_STATE_ADDED_NEXT_YEAR')).toBeNull()
  })

  it('never derives a string from the code itself', () => {
    const invented = level1('opportunity', 'INCREASE_SOMETHING_NEW')
    expect(invented).toBeNull()
    /* Explicitly NOT "Increase something new" — no transformation of the
       identifier is an acceptable answer. */
    expect(invented).not.toBe('Increase something new')
  })

  it('is null-safe for absent values', () => {
    for (const vocabulary of VOCABULARIES) {
      expect(describeCode(vocabulary, null)).toBeNull()
      expect(describeCode(vocabulary, undefined)).toBeNull()
      expect(describeCode(vocabulary, '')).toBeNull()
    }
  })

  it('does not leak one vocabulary into another', () => {
    /* `READY` exists in three vocabularies and means three different things.
       A lookup answers for the vocabulary it was asked about, and a code from
       elsewhere does not resolve. */
    expect(level1('urgency', 'EVIDENCE_REQUIRED')).toBeNull()
    expect(level1('family', 'URGENT')).toBeNull()
    expect(level1('assuranceStatus', 'RRSP_ROOM')).toBeNull()
  })
})

suite('formal Canadian tax names keep their capitals', () => {
  const ACRONYMS = ['RRSP', 'FHSA', 'TFSA', 'CRA', 'T4', 'T2202']

  it('never lowercases an acronym in any level', () => {
    const wrong = ACRONYMS.map((a) => a.charAt(0) + a.slice(1).toLowerCase())
    for (const [vocabulary, code, term] of everyTerm()) {
      for (const text of [term.level1, term.level2, term.technical]) {
        for (const bad of wrong) {
          expect(
            text.includes(bad),
            `${vocabulary}.${code} contains "${bad}" — a formal name was ` +
              `case-folded. This is the defect humanize() introduced: RRSP is a ` +
              `proper name, and "Rrsp" is a spelling mistake in a tax product.`,
          ).toBe(false)
        }
      }
    }
  })

  it('spells RRSP and FHSA correctly where they appear', () => {
    expect(OPPORTUNITY_CODE.INCREASE_RRSP_DEDUCTION.level1).toContain('RRSP')
    expect(OPPORTUNITY_CODE.INCREASE_RRSP_DEDUCTION.technical).toContain('RRSP')
    expect(RESOURCE_CODE.RRSP_ROOM.level1).toContain('RRSP')
    expect(RESOURCE_CODE.FHSA_ROOM.level1).toContain('FHSA')
  })

  it('pairs an acronym with what it stands for at Level 2', () => {
    /* A beginner who does not know the acronym is not helped by seeing it
       again. Level 1 may use the formal name; Level 2 has to expand it. */
    expect(OPPORTUNITY_CODE.INCREASE_RRSP_DEDUCTION.level2).toContain(
      'Registered Retirement Savings Plan',
    )
    expect(OPPORTUNITY_CODE.INCREASE_FHSA_DEDUCTION.level2).toContain(
      'First Home Savings Account',
    )
  })
})

suite('Level 1 is short, Level 2 explains, and the formal term survives', () => {
  it('gives every non-suppressed state a short Level 1', () => {
    for (const [vocabulary, code, term] of everyTerm()) {
      if (term.suppress) continue
      expect(term.level1.length, `${vocabulary}.${code} Level 1 is empty`).toBeGreaterThan(0)
      expect(
        term.level1.length,
        `${vocabulary}.${code} Level 1 is ${term.level1.length} characters — Level 1 is ` +
          `a label, not an explanation. Move the detail to Level 2.`,
      ).toBeLessThanOrEqual(40)
    }
  })

  it('gives every non-suppressed state a Level 2 that says more than Level 1', () => {
    for (const [vocabulary, code, term] of everyTerm()) {
      if (term.suppress) continue
      expect(term.level2.length, `${vocabulary}.${code} Level 2 is empty`).toBeGreaterThan(
        term.level1.length,
      )
    }
  })

  it('keeps a formal term for every state, suppressed ones included', () => {
    for (const [vocabulary, code, term] of everyTerm()) {
      expect(
        term.technical.length,
        `${vocabulary}.${code} has no technical term. A term is demoted, never deleted — ` +
          `an auditor reading the same screen needs the precise name.`,
      ).toBeGreaterThan(0)
    }
  })

  it('never puts a raw code in consumer-visible text', () => {
    const RAW = /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/
    for (const [vocabulary, code, term] of everyTerm()) {
      expect(RAW.test(term.level1), `${vocabulary}.${code} Level 1 contains a raw code`).toBe(false)
      expect(RAW.test(term.level2), `${vocabulary}.${code} Level 2 contains a raw code`).toBe(false)
    }
  })

  it('suppresses states that describe Onyx rather than the customer', () => {
    const term = REASON_CODE.RESERVED_NO_PRODUCER
    expect(term.suppress).toBe(true)
    expect(level1('reason', 'RESERVED_NO_PRODUCER')).toBeNull()
    expect(level2('reason', 'RESERVED_NO_PRODUCER')).toBeNull()
    // …and the formal term is still there for the advanced view.
    expect(technical('reason', 'RESERVED_NO_PRODUCER')).toBe('Reserved — no producer')
  })
})

suite('the states the audit named, read the way a beginner would', () => {
  it('says what Onyx needs instead of naming an internal state', () => {
    expect(ACTION_STATUS.EVIDENCE_REQUIRED.level1).toBe('We need a document')
    expect(ACTION_STATUS.DECISION_REQUIRED.level1).toBe('We need you to choose')
    expect(REASON_CODE.ELIGIBILITY_INDETERMINATE.level1).toBe("We can't confirm this yet")
  })

  it('does not report an unanswered question as a clean result', () => {
    /* The engine distinguishes "nothing found" from "not looked at yet", and
       the consumer wording has to keep that distinction — it is the reason the
       existing empty-state copy is trustworthy. */
    expect(ASSURANCE_STATUS.UNAVAILABLE.level1).not.toBe(ASSURANCE_STATUS.NOT_APPLICABLE.level1)
    expect(ASSURANCE_STATUS.UNAVAILABLE.level2).toContain('different')
  })

  it('keeps urgency separate from status, as the engine does', () => {
    expect(URGENCY_STATUS.URGENT.level1).not.toBe(ASSURANCE_STATUS.EVIDENCE_REQUIRED.level1)
    expect(EVIDENCE_READINESS.PARTIAL.level1).toBe('We need one more thing')
  })

  it('names the ten families in words a person could use', () => {
    expect(ASSURANCE_FAMILY.TAX_STATE.level1).toBe('Your numbers')
    expect(ASSURANCE_FAMILY.EVIDENCE.level1).toBe('Documents')
    expect(ASSURANCE_FAMILY.RESOURCE.level1).toBe('Room you have')
  })
})
