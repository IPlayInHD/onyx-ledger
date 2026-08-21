/* =========================================================================
   TRUST LAYER TESTS
   =========================================================================
   These assert the promises the product makes to a customer about where a
   statement came from. They are the frontend equivalent of the backend's
   security invariants: if one of these fails, the interface is misrepresenting
   provenance, and that is a correctness failure rather than a cosmetic one.
   ========================================================================= */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  AiExplanation,
  AssumptionNote,
  EligibilityBadge,
  FreshnessBadge,
  IntegrityBadge,
  Provenance,
  SupportSignal,
  TrustPair,
} from './trust'

describe('provenance badges', () => {
  it('names every kind in words, not only in colour', () => {
    // A colour-only system is unreadable to a colour-blind customer and
    // invisible in forced-colours mode, so the word is the actual carrier.
    const kinds = [
      ['calculated', 'Calculated'],
      ['governed', 'Governed source'],
      ['user', 'You provided'],
      ['assumption', 'Assumption'],
      ['ai', 'AI explanation'],
    ] as const
    for (const [kind, label] of kinds) {
      const { unmount } = render(<Provenance kind={kind} />)
      expect(screen.getByText(label)).toBeInTheDocument()
      unmount()
    }
  })
})

describe('assumption provenance', () => {
  it('describes a platform default as an assumption to confirm, never as known room', () => {
    render(
      <AssumptionNote
        assumption={{
          assumption_code: 'CONTRIBUTION_ROOM_AVAILABLE',
          certainty: 'platform_default',
          source: 'platform',
        }}
        formattedValue="$5,000.00"
      />,
    )
    const text = document.body.textContent ?? ''
    expect(text).toMatch(/assumes/i)
    expect(text).toMatch(/confirm/i)
    // The exact failure this guards against: telling someone they HAVE room
    // that nobody verified.
    expect(text).not.toMatch(/you (have|entered|told us)/i)
    // Affirmative verification claims only. "has not verified" is the honest
    // sentence this copy is built around, so the assertion has to distinguish
    // a claim from its negation rather than banning the word outright.
    expect(text).not.toMatch(/\b(is|was|has been|we) verified\b/i)
    expect(text).not.toMatch(/confirmed room|on file/i)
  })

  it('describes a user-stated value as the customer own declaration', () => {
    render(
      <AssumptionNote
        assumption={{
          assumption_code: 'CONTRIBUTION_ROOM_AVAILABLE',
          certainty: 'user_asserted',
          source: 'user',
        }}
        formattedValue="$12,000.00"
      />,
    )
    const text = document.body.textContent ?? ''
    expect(text).toMatch(/you told us/i)
    expect(text).not.toMatch(/published limit|statutory/i)
  })

  it('describes a statutory value as a published limit', () => {
    render(
      <AssumptionNote
        assumption={{
          assumption_code: 'CONTRIBUTION_ROOM_AVAILABLE',
          certainty: 'statutory_known',
          source: 'platform',
        }}
        formattedValue="$8,000.00"
      />,
    )
    expect(document.body.textContent).toMatch(/published limit/i)
  })

  it('falls back to the most cautious reading for an unknown certainty', () => {
    // An unrecognised certainty must never be treated as a known fact.
    render(
      <AssumptionNote
        assumption={{ assumption_code: 'SOMETHING_NEW', certainty: 'who_knows' }}
        formattedValue="$1.00"
      />,
    )
    expect(document.body.textContent).toMatch(/assumes/i)
  })
})

describe('freshness and integrity are never merged', () => {
  it('labels the two questions separately', () => {
    render(<TrustPair freshness="stale" integrity="verified" />)
    expect(screen.getByText('Still current?')).toBeInTheDocument()
    expect(screen.getByText('Reproducible?')).toBeInTheDocument()
    // A stale result that still reproduces is a real and important combination:
    // both statements must survive being rendered together.
    expect(screen.getByText('May be out of date')).toBeInTheDocument()
    expect(screen.getByText('Reproduced')).toBeInTheDocument()
  })

  it('never calls an unverified result verified', () => {
    for (const state of ['non_reproducible', 'unavailable', 'legacy_unverifiable', 'not_checked']) {
      const { unmount } = render(<IntegrityBadge state={state} />)
      expect(screen.queryByText('Reproduced')).not.toBeInTheDocument()
      unmount()
    }
  })

  it('does not describe an unavailable check as corruption', () => {
    render(<IntegrityBadge state="unavailable" />)
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/corrupt|tamper|damaged/i)
  })

  it('treats an unknown freshness value as not evaluated, never as current', () => {
    render(<FreshnessBadge status="something_else" />)
    expect(screen.getByText('Not evaluated')).toBeInTheDocument()
  })
})

describe('support signal', () => {
  it('never presents support as a probability or a percentage', () => {
    render(<SupportSignal support={{ display_support_score: '74' }} />)
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/%/)
    expect(text).not.toMatch(/probability|likelihood|chance/i)
    expect(text).toMatch(/not a prediction that the CRA will accept/i)
  })

  it('surfaces a cap when the backend applied one', () => {
    render(
      <SupportSignal
        support={{
          display_support_score: '40',
          support_cap_applied: true,
          support_cap_reason_code: 'ASSUMPTION_DEPENDENT',
        }}
      />,
    )
    expect(document.body.textContent).toMatch(/limited because assumptions/i)
  })

  it('renders nothing when the backend supplied no score', () => {
    const { container } = render(<SupportSignal support={{}} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('eligibility is never upgraded', () => {
  it('does not produce affirmative language for non-eligible states', () => {
    for (const status of ['ineligible', 'indeterminate', 'conditionally_eligible']) {
      const { unmount } = render(<EligibilityBadge status={status} />)
      const text = document.body.textContent ?? ''
      expect(text).not.toMatch(/you qualify|you are eligible/i)
      unmount()
    }
  })

  it('treats an unknown status as undetermined rather than eligible', () => {
    render(<EligibilityBadge status={undefined} />)
    expect(screen.getByText('Not determined')).toBeInTheDocument()
  })
})

describe('AI explanation framing', () => {
  const explanation = {
    summary: 'Your estimated tax for 2025 is $9,348.85 on taxable income of $60,000.00.',
    limitations: 'Educational information only.',
  }

  it('is permanently labelled as AI-written', () => {
    render(<AiExplanation explanation={explanation} />)
    expect(screen.getByText(/AI explanation/i)).toBeInTheDocument()
  })

  it('states that the model did not decide any amount', () => {
    render(<AiExplanation explanation={explanation} />)
    expect(document.body.textContent).toMatch(/does not decide any amount/i)
  })

  it('renders model text as inert content, never as markup', () => {
    // Model output is untrusted by definition. React escaping is the whole
    // protection here, and this asserts there is no HTML path to misconfigure.
    render(
      <AiExplanation
        explanation={{
          ...explanation,
          summary: '<img src=x onerror="alert(1)"> and <b>bold</b>',
        }}
      />,
    )
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('b')).toBeNull()
    expect(document.body.textContent).toContain('<img src=x onerror="alert(1)">')
  })

  it('never tells the customer which renderer produced the text', () => {
    render(<AiExplanation explanation={explanation} />)
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/fallback|renderer_mode|degraded|unavailable model/i)
  })
})
