/* =========================================================================
   TAX ASSURANCE OVERVIEW  —  the signature surface
   =========================================================================
   Not a dashboard of KPI tiles. It answers, in one screen and in this order:

     where do I stand      → the position headline, stated once, loudly
     is it still true      → freshness AND integrity, side by side, never merged
     what needs me         → the attention list, in the backend's own order
     what does Onyx know   → the Assurance Ledger, family by family

   Everything is read from the certified backend. The only thing this file
   decides is presentation order, and even the attention order comes from the
   backend's `attention` array rather than being re-ranked here.
   ========================================================================= */
import { Link } from 'react-router-dom'
import { PageHead, useTaxYear } from '@/components/Shell'
import {
  AsyncBlock,
  EmptyState,
  ErrorState,
  LoadingBlock,
} from '@/components/states'
import { Provenance, TrustPair } from '@/components/trust'
import { money, percent } from '@/lib/format'
import { level1, technical } from '@/lib/lexicon'
import {
  latestAnalysisFor,
  useAnalyses,
  useAssurance,
  useChanges,
  useRunAnalysis,
} from '@/lib/queries'
import type { TaxAssuranceOut } from '@/api/endpoints'

/* Map the backend's closed assurance vocabulary onto the ledger's stroke
   styles. An unknown status falls through to `unavailable`, the most cautious
   reading — never to `ready`, which would claim something nobody verified. */
/* STROKE STYLE ONLY. What each status is CALLED now comes from the consumer
   lexicon, so this table no longer carries words — it carries the two class
   names that decide how a row is drawn. An unknown status still falls through
   to `unavailable`, the most cautious reading, never to `ready`. */
const FAMILY_TONE: Record<string, { rule: string; status: string }> = {
  READY: { rule: 'ready', status: 'ready' },
  EVIDENCE_REQUIRED: { rule: 'attention', status: 'attention' },
  REVIEW_REQUIRED: { rule: 'attention', status: 'attention' },
  BLOCKED: { rule: 'blocked', status: 'blocked' },
  UNAVAILABLE: { rule: 'unavailable', status: 'neutral' },
  NOT_APPLICABLE: { rule: 'unavailable', status: 'neutral' },
}

function familyTone(status: string) {
  return FAMILY_TONE[status] ?? FAMILY_TONE['UNAVAILABLE']!
}

function AssuranceLedger({ assurance }: { assurance: TaxAssuranceOut }) {
  const families = assurance.families ?? []
  if (families.length === 0) {
    return (
      <EmptyState
        title="Nothing to map yet"
        body="Once an analysis has run for this tax year, Onyx will show what it knows about each part of your position."
      />
    )
  }

  return (
    <>
      {/* The visual ledger. It is marked presentational and paired with the
          table below, which carries the same facts for assistive technology
          and for anyone who would rather read numbers than strokes. */}
      <div className="assurance" aria-hidden="true">
        {families.map((family) => {
          const tone = familyTone(family.status)
          return (
            <div className="afam" key={family.family}>
              <span className="afam__name">
                {level1('family', family.family) ?? technical('family', family.family)}
              </span>
              <span className="afam__count">
                {family.item_count} {family.item_count === 1 ? 'item' : 'items'}
              </span>
              <span className={`afam__rule afam__rule--${tone.rule}`} />
              <span className="afam__meta">
                <span className={`status status--${tone.status}`}>
                  {level1('assuranceStatus', family.status) ??
                    technical('assuranceStatus', family.status)}
                </span>
                {/* A reason the customer can act on, or nothing. `describe`
                    returns null for a state nobody has written words for and
                    for states that describe Onyx rather than the customer —
                    `RESERVED_NO_PRODUCER` was reaching a brand-new account's
                    first screen as "Reserved no producer". */}
                {level1('reason', family.reason_code) ? (
                  <span className="text-xs text-muted">
                    {level1('reason', family.reason_code)}
                  </span>
                ) : null}
              </span>
            </div>
          )
        })}
      </div>

      <table className="data-table sr-only">
        <caption>Assurance state by family</caption>
        <thead>
          <tr>
            <th scope="col">Family</th>
            <th scope="col">State</th>
            <th scope="col">Why</th>
            <th scope="col">Items</th>
          </tr>
        </thead>
        <tbody>
          {families.map((family) => (
            <tr key={family.family}>
              <th scope="row">
                {level1('family', family.family) ?? technical('family', family.family)}
              </th>
              <td>
                {level1('assuranceStatus', family.status) ??
                  technical('assuranceStatus', family.status)}
              </td>
              {/* THE SAME WORDS THE VISUAL LEDGER SHOWS. The first version of
                  this used the technical term here, which quietly handed a
                  screen-reader user "Reserved — no producer" on a screen where
                  a sighted user was shown nothing at all. This table is the
                  accessible EQUIVALENT of the strokes above, so it says what
                  they say. The formal term is reachable through the Level 4
                  disclosure that the progressive-disclosure entry adds — it is
                  demoted, not deleted, and `technical()` is already tested. */}
              <td>{level1('reason', family.reason_code) ?? 'Not stated'}</td>
              <td className="numeric">{family.item_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function AttentionList({ assurance }: { assurance: TaxAssuranceOut }) {
  const byId = new Map(assurance.opportunities.map((o) => [o.source_id, o]))
  const attention = (assurance.attention ?? []).slice(0, 6)

  if (attention.length === 0) {
    return (
      <EmptyState
        title="Nothing is waiting on you"
        body="Onyx has not found anything in this tax year that needs a decision or a document right now."
      />
    )
  }

  return (
    <div className="attention">
      {attention.map((sourceId, index) => {
        const item = byId.get(sourceId)
        if (!item) return null
        return (
          <Link
            className="attention__item"
            key={sourceId}
            to={`/app/opportunities/${encodeURIComponent(sourceId)}`}
          >
            <span className="attention__rank">{index + 1}</span>
            <span>
              <span className="attention__title">
                {level1('opportunity', item.opportunity_code) ??
                  technical('opportunity', item.opportunity_code) ??
                  'Something worth checking'}
              </span>
              <span className="attention__sub">
                {level1('action', item.action) ?? technical('action', item.action)}
                {item.deadline
                  ? ` · ${item.deadline.days_remaining} days remaining`
                  : ''}
              </span>
            </span>
            <span className="row row-2">
              {item.assumption_dependent ? (
                <Provenance kind="assumption" label="Assumption" />
              ) : null}
              <span className={`status status--${familyTone(item.status).status}`}>
                {level1('urgency', item.urgency) ?? technical('urgency', item.urgency)}
              </span>
            </span>
          </Link>
        )
      })}
    </div>
  )
}

export default function Overview() {
  const { taxYear } = useTaxYear()
  const analyses = useAnalyses()
  const assurance = useAssurance(taxYear)
  const changes = useChanges(taxYear)
  const runAnalysis = useRunAnalysis()

  const analysis = latestAnalysisFor(analyses.data, taxYear)

  return (
    <>
      <PageHead
        eyebrow={`Tax year ${taxYear}`}
        /* Not "Your tax position": that is the name of a DIFFERENT destination
           in the navigation, and heading a screen with the label of the screen
           beside it leaves a reader unsure which one they are on. */
        title="Where you stand"
        lede="What Onyx is confident about today, what it is still assuming, and what is waiting on you."
        actions={
          <button
            type="button"
            className="btn btn--secondary"
            onClick={() => runAnalysis.mutate(taxYear)}
            disabled={runAnalysis.isPending}
          >
            {runAnalysis.isPending ? 'Running…' : 'Re-run analysis'}
          </button>
        }
      />

      {runAnalysis.isError ? (
        <div className="mb-5">
          <ErrorState error={runAnalysis.error} />
        </div>
      ) : null}

      <div className="stack stack-6">
        {/* ---------------------------------------------- position headline */}
        <section className="panel" aria-labelledby="position-heading">
          <div className="panel__header">
            <h2 className="section-title" id="position-heading">
              Estimated position
            </h2>
            <Link className="btn btn--ghost btn--sm" to="/app/position">
              See the breakdown
            </Link>
          </div>
          <div className="panel__body">
            {analyses.isPending ? (
              <LoadingBlock label="Loading your position" />
            ) : analyses.isError ? (
              <ErrorState error={analyses.error} onRetry={analyses.refetch} />
            ) : !analysis ? (
              <EmptyState
                title="No analysis yet for this year"
                body="Add your income and other facts, and Onyx will work out where you stand."
                action={
                  <Link className="btn btn--primary" to="/app/onboarding">
                    Add your details
                  </Link>
                }
              />
            ) : (
              <div className="headline">
                <div className="headline__figure">
                  <span className="eyebrow">Estimated tax for {taxYear}</span>
                  <span className="figure figure--hero">
                    {money(analysis.estimated_tax)}
                  </span>
                  <div className="headline__meta">
                    <Provenance kind="calculated" />
                    <span className="text-xs text-muted">
                      Engine {analysis.engine_version}
                    </span>
                  </div>
                </div>

                <div className="figrow figrow--3">
                  <div className="figrow__cell">
                    <span className="figrow__label">Taxable income</span>
                    <span className="figure figure--sm tabular">
                      {money(analysis.taxable_income)}
                    </span>
                  </div>
                  <div className="figrow__cell">
                    <span className="figrow__label">Marginal rate</span>
                    <span className="figure figure--sm tabular">
                      {percent(analysis.marginal_rate)}
                    </span>
                  </div>
                  <div className="figrow__cell">
                    <span className="figrow__label">Average rate</span>
                    <span className="figure figure--sm tabular">
                      {percent(analysis.average_rate)}
                    </span>
                  </div>
                </div>
              </div>
            )}
          </div>

          {analysis ? (
            <div className="panel__footer">
              {/* Freshness and integrity, always separate. The assurance map
                  carries per-item states; this pair describes the run. */}
              <TrustPair
                freshness={assurance.data ? 'current' : 'unknown'}
                integrity="not_checked"
              />
            </div>
          ) : null}
        </section>

        {/* ------------------------------------------------------ attention */}
        <section className="panel" aria-labelledby="attention-heading">
          <div className="panel__header">
            <h2 className="section-title" id="attention-heading">
              Waiting on you
            </h2>
            <Link className="btn btn--ghost btn--sm" to="/app/opportunities">
              All opportunities
            </Link>
          </div>
          <div className="panel__body">
            <AsyncBlock query={assurance}>
              {(data) => <AttentionList assurance={data} />}
            </AsyncBlock>
          </div>
        </section>

        {/* ------------------------------------------- the assurance ledger */}
        <section className="panel" aria-labelledby="ledger-heading">
          <div className="panel__header">
            <div>
              <h2 className="section-title" id="ledger-heading">
                Assurance ledger
              </h2>
              <p className="text-sm text-muted mt-1">
                What Onyx can and cannot currently stand behind, family by family.
              </p>
            </div>
          </div>
          <div className="panel__body">
            <AsyncBlock query={assurance}>
              {(data) => <AssuranceLedger assurance={data} />}
            </AsyncBlock>
          </div>
          <div className="panel__footer">
            <p className="text-xs text-muted measure">
              A family shown as <strong>Not established yet</strong> means no
              governing run has produced it — which is different from it being
              empty. Onyx does not report an unanswered question as a clean
              result.
            </p>
          </div>
        </section>

        {/* -------------------------------------------------- recent change */}
        <section className="panel" aria-labelledby="changes-heading">
          <div className="panel__header">
            <h2 className="section-title" id="changes-heading">
              Recently changed
            </h2>
            <Link className="btn btn--ghost btn--sm" to="/app/changes">
              Full history
            </Link>
          </div>
          <div className="panel__body">
            <AsyncBlock query={changes}>
              {(data) =>
                data.changes.length === 0 ? (
                  <EmptyState
                    title="Nothing material has changed"
                    body={
                      data.baseline_status === 'NO_BASELINE'
                        ? 'Once you acknowledge your current position, Onyx will track what moves from that point.'
                        : 'Nothing has moved in your tax picture since the state you last reviewed.'
                    }
                  />
                ) : (
                  <p className="text-sm text-secondary">
                    {data.summary.total}{' '}
                    {data.summary.total === 1 ? 'change' : 'changes'} since the
                    state you last reviewed.
                  </p>
                )
              }
            </AsyncBlock>
          </div>
        </section>
      </div>
    </>
  )
}
