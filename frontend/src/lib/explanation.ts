/* =========================================================================
   AI EXPLANATIONS — the only client-side route to one
   =========================================================================
   The browser asks the BACKEND to explain a subject the customer already owns.
   It never talks to a model provider, never holds a provider credential, and
   never ships tax data anywhere except back to Onyx.

   Explanations are fetched ON DEMAND (`enabled` gates the query) rather than
   eagerly with every screen: an explanation is admission-controlled work with
   a real cost, and pre-fetching one for a panel nobody expands spends the
   customer's own AI_EXPLAIN budget for nothing.
   ========================================================================= */
import { useQuery } from '@tanstack/react-query'
import { aiApi, type ExplanationType } from '@/api/endpoints'

export function explanationKey(
  type: ExplanationType,
  subjectId: string | null | undefined,
  taxYear: number | null | undefined,
) {
  return ['explanation', type, subjectId ?? null, taxYear ?? null] as const
}

/**
 * `enabled` is what makes this on-demand: a panel passes `false` until the
 * customer actually asks for the explanation.
 *
 * Retries are disabled. A refused explanation is usually a deliberate refusal
 * (throttled, or a subject that is not the caller's), and asking again
 * immediately would spend budget to receive the same answer.
 */
export function useExplanation(args: {
  type: ExplanationType
  subjectId?: string | null
  taxYear?: number | null
  enabled: boolean
}) {
  const { type, subjectId, taxYear, enabled } = args
  return useQuery({
    queryKey: explanationKey(type, subjectId, taxYear),
    queryFn: () =>
      aiApi.explain({
        explanation_type: type,
        subject_id: subjectId ?? null,
        tax_year: taxYear ?? null,
      }),
    enabled,
    retry: false,
    // An explanation describes a specific sealed subject, so it does not go
    // stale on a timer the way a live position does.
    staleTime: 5 * 60_000,
  })
}
