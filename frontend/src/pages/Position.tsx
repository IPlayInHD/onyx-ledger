/* =========================================================================
   TAX POSITION  —  the estimate, then its lineage
   =========================================================================
   The Overview states where you stand. This screen answers the next question a
   person actually asks, in this order:

     what is the number      → the estimate, once, in the display serif
     what is behind it       → the breakdown, IF the engine published one
     where did it come from  → calculated value, governed data, your own facts
     say it in words         → an explanation, only when asked for

   Two absences are deliberate and are the reason this file reads the way it
   does.

   `GET /analysis` returns the position as SUMMARY figures. It carries no line
   items and no total income, so this screen shows neither. The engine does
   compute a federal / provincial / CPP split — it is simply not in this
   response — and reassembling one in the browser would put a number in front
   of a customer that no certified run ever produced. The breakdown panel says
   that plainly instead.
   ========================================================================= */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { PageHead, useTaxYear } from '@/components/Shell'
import { EmptyState, ErrorState, LoadingBlock } from '@/components/states'
import { AiExplanation, Provenance } from '@/components/trust'
import { isoDate, money, percent } from '@/lib/format'
import { useExplanation } from '@/lib/explanation'
import { latestAnalysisFor, useAnalyses } from '@/lib/queries'
import type { AnalysisOut } from '@/api/endpoints'

/* ------------------------------------------------------------- headline -- */

function PositionHeadline({
  analysis,
  taxYear,
}: {
  analysis: AnalysisOut
  taxYear: number
}) {
  return (
    <div className="headline">
      <div className="headline__figure">
        <span className="eyebrow">Estimated tax for {taxYear}</span>
        <span className="figure figure--hero tabular">
          {money(analysis.estimated_tax)}
        </span>
        <div className="headline__meta">
          <Provenance kind="calculated" />
          <span className="text-xs text-muted">
            Engine {analysis.engine_version}
          </span>
          {analysis.province_code ? (
            <span className="text-xs text-muted">
              Province {analysis.province_code}
            </span>
          ) : null}
        </div>
      </div>

      {/* Taxable income, and the two rates. Total income belongs beside these
          and is not in the response — see the breakdown panel. */}
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
  )
}

/* ------------------------------------------------------------ provenance -- */

/** The chain behind the headline, drawn as a sequence rather than as three
 *  unrelated badges. Each node names the KIND of authority it rests on, and
 *  nothing here claims more than the analysis response actually states. */
function PositionTrace({
  analysis,
  taxYear,
}: {
  analysis: AnalysisOut
  taxYear: number
}) {
  return (
    <div className="trace">
      <div className="trace__node trace__node--calculated">
        <div className="trace__title">The figures above</div>
        <div className="stack stack-2">
          <div className="row row-2 wrap">
            <Provenance kind="calculated" />
          </div>
          <p className="trace__detail">
            Worked out by the Onyx tax engine, version{' '}
            {analysis.engine_version}, on {isoDate(analysis.created_at)}. They
            are an estimate of your {taxYear} position, not a filing, and
            nothing here is submitted to the Canada Revenue Agency.
          </p>
        </div>
      </div>

      <div className="trace__node trace__node--governed">
        <div className="trace__title">The rates and thresholds it used</div>
        <div className="stack stack-2">
          <div className="row row-2 wrap">
            <Provenance kind="governed" />
          </div>
          <p className="trace__detail">
            The brackets, credits and limits in force for {taxYear}
            {analysis.province_code ? ` in ${analysis.province_code}` : ''} come
            from published tax sources that Onyx keeps under version control.
            The engine will not run for a year or a province it has no published
            data for; it stops rather than estimating around the gap.
          </p>
        </div>
      </div>

      <div className="trace__node trace__node--user">
        <div className="trace__title">The facts you recorded</div>
        <div className="stack stack-2">
          <div className="row row-2 wrap">
            <Provenance kind="user" />
          </div>
          <p className="trace__detail">
            Your income, accounts and profile details are used exactly as you
            entered them — Onyx does not adjust or infer them. This result
            reflects those facts as they stood when it ran, so if something has
            changed since, run the analysis again.{' '}
            <Link to="/app/onboarding">
              Review the details you have given Onyx
            </Link>
            .
          </p>
        </div>
      </div>
    </div>
  )
}

/* -------------------------------------------------------- AI explanation -- */

/**
 * On demand, never eagerly: an explanation is admission-controlled work with a
 * real cost, and nobody's budget should be spent on a panel they did not open.
 *
 * `renderer_mode` is never mentioned. When the deterministic renderer answers
 * instead of the model, the customer is reading a perfectly good explanation
 * and the product has nothing to apologise for.
 */
function PositionExplanation({ analysisId }: { analysisId: string }) {
  const [asked, setAsked] = useState(false)
  const explanation = useExplanation({
    type: 'TAX_POSITION',
    subjectId: analysisId,
    enabled: asked,
  })

  return (
    <div className="stack stack-4">
      <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
        Onyx can put this position into words: what drives the estimate and what
        it depends on. The figures do not change — the wording is written from
        the same ones shown above.
      </p>

      {/* A toggle rather than a one-way switch: hiding and re-opening reads
          from the cache, so a customer can put the panel away without spending
          their explanation budget again to get it back. */}
      <div>
        <button
          type="button"
          className="btn btn--secondary"
          onClick={() => setAsked((value) => !value)}
          aria-expanded={asked}
          aria-controls="position-explanation"
        >
          {asked ? 'Hide explanation' : 'Explain this position'}
        </button>
      </div>

      <div id="position-explanation">
        {!asked ? null : explanation.isPending ? (
          <LoadingBlock label="Writing an explanation of your position" />
        ) : explanation.isError ? (
          <ErrorState
            error={explanation.error}
            onRetry={() => void explanation.refetch()}
          />
        ) : (
          <AiExplanation explanation={explanation.data.explanation} />
        )}
      </div>
    </div>
  )
}

/* -------------------------------------------------------------- the page -- */

export default function Position() {
  const { taxYear } = useTaxYear()
  const analyses = useAnalyses()
  const analysis = latestAnalysisFor(analyses.data, taxYear)

  return (
    <>
      <PageHead
        eyebrow={`Tax year ${taxYear}`}
        title="Your position in detail"
        lede="The figures Onyx calculated for this year, and where each one came from."
      />

      <div className="stack stack-6">
        {/* ------------------------------------------------- the estimate */}
        <section className="panel" aria-labelledby="estimate-heading">
          <div className="panel__header">
            <h2 className="section-title" id="estimate-heading">
              The estimate
            </h2>
          </div>
          <div className="panel__body">
            {analyses.isPending ? (
              <LoadingBlock label="Loading your position" />
            ) : analyses.isError ? (
              <ErrorState
                error={analyses.error}
                onRetry={() => void analyses.refetch()}
              />
            ) : !analysis ? (
              <EmptyState
                title="No analysis for this year yet"
                body="Once you have recorded your income and other details, Onyx can work out where you stand for this tax year."
                action={
                  <Link className="btn btn--primary" to="/app/onboarding">
                    Add your details
                  </Link>
                }
              />
            ) : (
              <PositionHeadline analysis={analysis} taxYear={taxYear} />
            )}
          </div>
          {analysis ? (
            <div className="panel__footer">
              <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
                An estimate produced on {isoDate(analysis.created_at)}. It is
                information about your position, not tax advice, and it is not a
                return.
              </p>
            </div>
          ) : null}
        </section>

        {analysis ? (
          <>
            {/* ------------------------------------------------- breakdown */}
            <section className="panel" aria-labelledby="breakdown-heading">
              <div className="panel__header">
                <h2 className="section-title" id="breakdown-heading">
                  Breakdown
                </h2>
              </div>
              <div className="panel__body">
                {/* The engine does split this estimate into line items. This
                    endpoint does not return them, so there is nothing honest to
                    put in a table here — and a split assembled in the browser
                    would not be the engine's arithmetic. */}
                <EmptyState
                  title="A line-by-line breakdown is not available here"
                  body="Onyx sends this position to your browser as summary figures only. The federal and provincial components, and any contributions payable, are worked out by the tax engine but are not part of what this screen receives — and Onyx will not reconstruct them here, because a figure it assembled itself would not be the one the engine stands behind."
                />
              </div>
            </section>

            {/* ---------------------------------------------------- lineage */}
            <section className="panel" aria-labelledby="trace-heading">
              <div className="panel__header">
                <div>
                  <h2 className="section-title" id="trace-heading">
                    Where did this come from?
                  </h2>
                  <p
                    className="text-sm text-muted"
                    style={{ marginTop: 'var(--space-1)' }}
                  >
                    Three kinds of authority stand behind the estimate. They are
                    not interchangeable, so Onyx keeps them apart.
                  </p>
                </div>
              </div>
              <div className="panel__body">
                <PositionTrace analysis={analysis} taxYear={taxYear} />
              </div>
            </section>

            {/* --------------------------------------------- in plain words */}
            <section className="panel" aria-labelledby="explain-heading">
              <div className="panel__header">
                <h2 className="section-title" id="explain-heading">
                  In plain language
                </h2>
              </div>
              <div className="panel__body">
                <PositionExplanation analysisId={analysis.id} />
              </div>
            </section>
          </>
        ) : null}
      </div>
    </>
  )
}
