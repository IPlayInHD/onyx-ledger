/* =========================================================================
   SHARED QUERIES
   =========================================================================
   Every screen reads backend state through these hooks, so the same fact is
   fetched once, cached once, and invalidated in one place. A screen that
   fetched its own copy of the analysis could show a different position from
   the one next to it, which in a tax product is not a caching nuance — it is
   two different answers to "what do I owe".
   ========================================================================= */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  analysisApi,
  financialsApi,
  ioeApi,
  profileApi,
  type AnalysisOut,
} from '@/api/endpoints'

export const keys = {
  profile: ['profile'] as const,
  analyses: ['analyses'] as const,
  recommendations: (id: string) => ['recommendations', id] as const,
  assurance: (year: number) => ['assurance', year] as const,
  lifecycle: (year: number) => ['lifecycle', year] as const,
  changes: (year: number) => ['changes', year] as const,
  income: (year: number) => ['income', year] as const,
  registered: (year: number) => ['registered', year] as const,
  scenarios: ['scenarios'] as const,
  scenario: (id: string) => ['scenario', id] as const,
  comparison: (id: string) => ['comparison', id] as const,
  portfolio: (runId: string) => ['portfolio', runId] as const,
  journals: ['journals'] as const,
}

export function useProfile() {
  return useQuery({ queryKey: keys.profile, queryFn: profileApi.get })
}

export function useAnalyses() {
  return useQuery({ queryKey: keys.analyses, queryFn: analysisApi.list })
}

/** The analysis this screen is talking about: the most recent COMPLETED run
 *  for the selected year. Picking by `created_at` rather than by array order
 *  keeps it correct regardless of how the backend orders its list. */
export function latestAnalysisFor(
  analyses: AnalysisOut[] | undefined,
  taxYear: number,
): AnalysisOut | null {
  if (!analyses) return null
  const forYear = analyses.filter((a) => a.tax_year === taxYear)
  if (forYear.length === 0) return null
  return forYear.reduce((newest, candidate) =>
    new Date(candidate.created_at) > new Date(newest.created_at) ? candidate : newest,
  )
}

export function useAssurance(taxYear: number) {
  return useQuery({
    queryKey: keys.assurance(taxYear),
    queryFn: () => ioeApi.assurance(taxYear),
  })
}

export function useLifecycle(taxYear: number) {
  return useQuery({
    queryKey: keys.lifecycle(taxYear),
    queryFn: () => ioeApi.lifecycle(taxYear),
  })
}

export function useChanges(taxYear: number) {
  return useQuery({
    queryKey: keys.changes(taxYear),
    queryFn: () => ioeApi.changes(taxYear),
  })
}

export function useIncome(taxYear: number) {
  return useQuery({
    queryKey: keys.income(taxYear),
    queryFn: () => financialsApi.listIncome(taxYear),
  })
}

export function useRegisteredAccounts(taxYear: number) {
  return useQuery({
    queryKey: keys.registered(taxYear),
    queryFn: () => financialsApi.listRegisteredAccounts(taxYear),
  })
}

export function useScenarios() {
  return useQuery({ queryKey: keys.scenarios, queryFn: ioeApi.listScenarios })
}

export function useScenario(id: string | undefined) {
  return useQuery({
    queryKey: keys.scenario(id ?? ''),
    queryFn: () => ioeApi.scenario(id!),
    enabled: Boolean(id),
  })
}

export function useComparison(scenarioId: string | undefined) {
  return useQuery({
    queryKey: keys.comparison(scenarioId ?? ''),
    queryFn: () => ioeApi.comparison(scenarioId!),
    enabled: Boolean(scenarioId),
  })
}

export function useRecommendations(analysisId: string | undefined) {
  return useQuery({
    queryKey: keys.recommendations(analysisId ?? ''),
    queryFn: () => analysisApi.recommendations(analysisId!),
    enabled: Boolean(analysisId),
  })
}

/**
 * Run a fresh analysis.
 *
 * Never retried: the backend admits ONE active analysis per user per tax year
 * and answers a second concurrent request with 409. A retry would race the run
 * the customer is already waiting for.
 */
export function useRunAnalysis() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (taxYear: number) => analysisApi.run(taxYear),
    retry: false,
    onSuccess: (analysis) => {
      // A new analysis moves everything downstream of it.
      void queryClient.invalidateQueries({ queryKey: keys.analyses })
      void queryClient.invalidateQueries({ queryKey: keys.assurance(analysis.tax_year) })
      void queryClient.invalidateQueries({ queryKey: keys.lifecycle(analysis.tax_year) })
      void queryClient.invalidateQueries({ queryKey: keys.changes(analysis.tax_year) })
    },
  })
}

export function useCreateScenario() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ioeApi.createScenario,
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.scenarios })
    },
  })
}
