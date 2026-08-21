/* =========================================================================
   TYPED ENDPOINTS
   =========================================================================
   One function per backend operation. Response types are imported from
   `schema.ts`, which is GENERATED from the backend's own OpenAPI document —
   so the frontend cannot quietly disagree with the contract about what a
   scenario, an assurance map or an explanation contains. When the backend
   changes shape, the type check fails here rather than the UI rendering
   `undefined` at a customer.
   ========================================================================= */
import { request } from './client'
import type { components } from './schema'

type S = components['schemas']

/* ------------------------------------------------------------------ auth -- */

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
}

export const authApi = {
  register: (email: string, password: string) =>
    request<{ id: string; email: string }>('/auth/register', {
      method: 'POST',
      body: { email, password },
      anonymous: true,
    }),

  login: (email: string, password: string) =>
    request<TokenPair>('/auth/login', {
      method: 'POST',
      body: { email, password },
      anonymous: true,
    }),

  logout: () => request<void>('/auth/logout', { method: 'POST' }),

  me: () => request<{ id: string; email: string; status: string }>('/users/me'),
}

/* --------------------------------------------------------------- profile -- */

export type TaxProfile = S['TaxProfileOut']
export type TaxProfileIn = S['TaxProfileIn']

export const profileApi = {
  get: () => request<TaxProfile>('/users/me/tax-profile'),
  update: (body: TaxProfileIn) =>
    request<TaxProfile>('/users/me/tax-profile', { method: 'PUT', body }),
}

/* ------------------------------------------------------------ financials -- */

export type IncomeOut = S['IncomeOut']
export type RegisteredAccountOut = S['RegisteredAccountOut']

export const financialsApi = {
  listIncome: (taxYear: number) =>
    request<IncomeOut[]>('/financials/income', { query: { tax_year: taxYear } }),

  addIncome: (body: {
    tax_year: number
    income_type_code: string
    amount: string
    source_name?: string | null
  }) => request<IncomeOut>('/financials/income', { method: 'POST', body }),

  deleteIncome: (id: string, taxYear: number) =>
    request<{ status: string; already_deleted: boolean }>(
      `/financials/income/${encodeURIComponent(id)}`,
      { method: 'DELETE', query: { tax_year: taxYear } },
    ),

  addExpense: (body: {
    tax_year: number
    expense_category_code: string
    amount: string
    description?: string | null
  }) =>
    request<{ id: string; tax_year: number; amount: string }>(
      '/financials/expenses',
      { method: 'POST', body },
    ),

  listRegisteredAccounts: (taxYear: number) =>
    request<RegisteredAccountOut[]>('/financials/registered-accounts', {
      query: { tax_year: taxYear },
    }),

  addRegisteredAccount: (body: {
    tax_year: number
    registered_type: 'RRSP' | 'FHSA'
    contributions_ytd: string
    contribution_room?: string | null
    label?: string | null
  }) =>
    request<RegisteredAccountOut>('/financials/registered-accounts', {
      method: 'POST',
      body,
    }),
}

/* -------------------------------------------------------------- analysis -- */

export type AnalysisOut = S['AnalysisOut']
export type RecommendationOut = S['RecommendationOut']

export const analysisApi = {
  list: () => request<AnalysisOut[]>('/analysis'),
  run: (taxYear: number) =>
    request<AnalysisOut>('/analysis', { method: 'POST', body: { tax_year: taxYear } }),
  recommendations: (analysisId: string) =>
    request<RecommendationOut[]>('/recommendations', {
      query: { analysis_id: analysisId },
    }),
}

/* ------------------------------------------------------------------- ioe -- */

export type TaxAssuranceOut = S['TaxAssuranceOut']
export type OpportunityLifecycleOut = S['OpportunityLifecycleOut']
export type ScenarioDetailOut = S['ScenarioDetailOut']
export type ScenarioSummaryOut = S['ScenarioSummaryOut']
export type StrategyPortfolioOut = S['StrategyPortfolioOut']
export type BeforeYouActComparisonOut = S['BeforeYouActComparisonOut']
export type RetentionChangesOut = S['RetentionChangesOut']
export type OptimizationRunOut = S['OptimizationRunOut']
export type DecisionJournalDetailOut = S['DecisionJournalDetailOut']

export const ioeApi = {
  assurance: (taxYear: number) =>
    request<TaxAssuranceOut>('/ioe/assurance', { query: { tax_year: taxYear } }),

  lifecycle: (taxYear: number) =>
    request<OpportunityLifecycleOut>('/ioe/opportunity-lifecycle', {
      query: { tax_year: taxYear },
    }),

  changes: (taxYear: number) =>
    request<RetentionChangesOut>('/ioe/changes', { query: { tax_year: taxYear } }),

  acknowledgeChanges: (body: {
    snapshot_hash: string
    baseline_checkpoint_id: string | null
    request_id: string
  }) => request<unknown>('/ioe/changes/acknowledge', { method: 'POST', body }),

  optimize: (analysisId: string, resourceCapacities?: Record<string, string>) =>
    request<OptimizationRunOut>('/ioe/optimizations', {
      method: 'POST',
      body: {
        analysis_id: analysisId,
        ...(resourceCapacities ? { resource_capacities: resourceCapacities } : {}),
      },
    }),

  portfolio: (runId: string) =>
    request<StrategyPortfolioOut>(`/ioe/runs/${encodeURIComponent(runId)}/portfolio`),

  listScenarios: () => request<ScenarioSummaryOut[]>('/ioe/scenarios'),

  scenario: (id: string) =>
    request<ScenarioDetailOut>(`/ioe/scenarios/${encodeURIComponent(id)}`),

  /** Creating a scenario is an expensive, admission-controlled write. It is
   *  never retried automatically — see `shouldRetry` in the client. */
  createScenario: (body: {
    analysis_id: string
    levers: { lever_code: string; parameters: Record<string, string> }[]
    assumptions?: {
      assumption_code: string
      value_number?: string
      materiality?: string
    }[]
    label?: string | null
    note?: string | null
  }) => request<ScenarioDetailOut>('/ioe/scenarios', { method: 'POST', body }),

  comparison: (scenarioId: string) =>
    request<BeforeYouActComparisonOut>(
      `/ioe/scenarios/${encodeURIComponent(scenarioId)}/comparison`,
    ),

  openDecisionJournal: (scenarioId: string, requestId: string) =>
    request<DecisionJournalDetailOut>('/ioe/decision-journal', {
      method: 'POST',
      body: { scenario_id: scenarioId, request_id: requestId },
    }),

  decisionJournal: (journalId: string) =>
    request<DecisionJournalDetailOut>(
      `/ioe/decision-journal/${encodeURIComponent(journalId)}`,
    ),

  listDecisionJournals: () =>
    request<S['DecisionJournalSummaryOut'][]>('/ioe/decision-journal'),
}

/* ------------------------------------------------------------ explanation -- */

export type ExplanationEnvelope = S['ExplanationEnvelopeOut']

export type ExplanationType =
  | 'TAX_POSITION'
  | 'OPPORTUNITY'
  | 'PORTFOLIO'
  | 'SCENARIO'
  | 'COMPARISON'
  | 'EVIDENCE_READINESS'
  | 'WHAT_CHANGED'

export const aiApi = {
  /**
   * The ONLY route to an explanation.
   *
   * The browser never talks to a model provider: it names a subject it already
   * owns, and the backend assembles the input, runs its validators and returns
   * validated output (or its deterministic renderer's). No provider credential
   * exists in this bundle because no provider call is made from it.
   */
  explain: (body: {
    explanation_type: ExplanationType
    subject_id?: string | null
    tax_year?: number | null
  }) => request<ExplanationEnvelope>('/ai/explanations', { method: 'POST', body }),
}

/* ---------------------------------------------------------------- account -- */

export const accountApi = {
  deletionStatus: () => request<Record<string, unknown>>('/account/deletion'),
  requestDeletion: (requestId: string) =>
    request<Record<string, unknown>>('/account/deletion', {
      method: 'POST',
      body: { request_id: requestId },
    }),
}

/* ----------------------------------------------------------------- config -- */

export interface LaunchScope {
  tax_years: number[]
  provinces: { code: string; name: string }[]
}

/**
 * What Onyx currently offers.
 *
 * ANONYMOUS on purpose: onboarding needs the list before an account exists.
 *
 * The frontend used to keep its own copy of this — a hard-coded array of
 * provinces and another of tax years — and the two drifted immediately. The
 * copy offered Quebec, which the engine cannot answer for. There is now one
 * list, the backend owns it, and it is checked against the engine's own
 * resolved dataset on the backend side.
 */
export const configApi = {
  launchScope: () => request<LaunchScope>('/config/launch-scope', { anonymous: true }),
}
