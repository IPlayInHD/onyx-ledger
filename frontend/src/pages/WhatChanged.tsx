/* =========================================================================
   WHAT CHANGED  —  what moved since the state you last reviewed
   =========================================================================
   A ledger of material events, not a feed. Three rules hold this screen
   together, and all three come from the backend rather than from here:

     ORDER AND SEVERITY ARE GIVEN. The change set arrives ordered, and each
     change carries a severity the retention domain looked up in a published
     table. Nothing on this page re-ranks, re-scores or re-sorts it — a number
     the UI tuned would be a recommendation engine wearing a ledger's clothes.

     NO BASELINE IS NOT AN ERROR. With nothing acknowledged there is no prior
     authoritative state, so the change list is empty BY DESIGN. Reporting a
     customer's existing position as a list of arrivals would describe events
     that never happened, so the screen says plainly that tracking starts once
     the current state is acknowledged.

     ACKNOWLEDGING IS CONCURRENCY-CHECKED. The acknowledgement echoes the exact
     state that was read. If it moved while the page was open the backend
     refuses with 409, and this screen says so and reloads rather than
     recording that somebody reviewed changes they never saw.

   The snapshot hashes are the concurrency tokens. They are sent, never shown.
   ========================================================================= */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { PageHead, useTaxYear } from '@/components/Shell'
import {
  AsyncBlock,
  EmptyState,
  ErrorState,
  LoadingBlock,
  describeError,
} from '@/components/states'
import { AiExplanation } from '@/components/trust'
import { useExplanation } from '@/lib/explanation'
import { humanize, isoDate, plainNumber } from '@/lib/format'
import { keys, useChanges } from '@/lib/queries'
import { ApiError } from '@/api/client'
import { ioeApi, type RetentionChangesOut } from '@/api/endpoints'

type MaterialChange = RetentionChangesOut['changes'][number]
type ChangeSummary = RetentionChangesOut['summary']

/* ------------------------------------------------------------- vocabulary -- */

/**
 * Severity, as a node shape AND a word.
 *
 * The ledger spine offers two node treatments; CRITICAL borrows the high one
 * and is separated from HIGH by its word and its pill, never by hue alone. A
 * customer reading in monochrome or with colour-blind vision still gets the
 * distinction, which is the whole reason severity is written out.
 */
const SEVERITY: Record<string, { node: string; tone: string; label: string }> = {
  CRITICAL: { node: ' change--high', tone: 'blocked', label: 'Critical' },
  HIGH: { node: ' change--high', tone: 'attention', label: 'High' },
  MEDIUM: { node: ' change--medium', tone: 'info', label: 'Medium' },
  LOW: { node: '', tone: 'neutral', label: 'Low' },
}

function severityCopy(severity: string) {
  return (
    SEVERITY[severity] ?? { node: '', tone: 'neutral', label: humanize(severity) }
  )
}

/** The severity vocabulary's own order, used only to lay out a counts table.
 *  The change list itself is never reordered. */
const SEVERITY_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

const KIND_LABELS: Record<string, string> = {
  ADDED: 'Appeared',
  REMOVED: 'Went away',
  CHANGED: 'Moved',
}

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? humanize(kind)
}

/** The named counters the summary carries, in the order they are worth
 *  reading. Each one is a transparent count the backend produced. */
type NotableKey =
  | 'opportunities_added'
  | 'opportunities_removed'
  | 'newly_urgent'
  | 'newly_expired'
  | 'newly_blocked'
  | 'decision_changes'
  | 'execution_reports'
  | 'evidence_improvements'
  | 'evidence_regressions'
  | 'freshness_changes'
  | 'integrity_changes'

const NOTABLE: { key: NotableKey; label: string }[] = [
  { key: 'opportunities_added', label: 'Opportunities that appeared' },
  { key: 'opportunities_removed', label: 'Opportunities that went away' },
  { key: 'newly_urgent', label: 'Newly urgent' },
  { key: 'newly_expired', label: 'Newly expired' },
  { key: 'newly_blocked', label: 'Newly blocked' },
  { key: 'decision_changes', label: 'Decisions you changed' },
  { key: 'execution_reports', label: 'Actions you reported taking' },
  { key: 'evidence_improvements', label: 'Evidence that improved' },
  { key: 'evidence_regressions', label: 'Evidence that fell back' },
  { key: 'freshness_changes', label: 'Freshness that moved' },
  { key: 'integrity_changes', label: 'Reproducibility that moved' },
]

/* ------------------------------------------------------------- transitions -- */

function fieldLabel(field: string): string {
  return humanize(field)
}

/** A governed state word, or a governed date read as one. Nothing here is a
 *  money value: the retention contract carries no amounts at all. */
function transitionValue(
  field: string,
  value: string | null | undefined,
): string | null {
  if (value === null || value === undefined || value === '') return null
  return field.endsWith('_date') ? isoDate(value) : humanize(value)
}

function TransitionValue({
  field,
  value,
  absent,
}: {
  field: string
  value: string | null | undefined
  absent: string
}) {
  const shown = transitionValue(field, value)
  if (shown === null) {
    return (
      <>
        <span aria-hidden="true">—</span>
        <span className="sr-only">{absent}</span>
      </>
    )
  }
  return <>{shown}</>
}

/* --------------------------------------------------------------- timeline -- */

function ChangeTimeline({ changes }: { changes: MaterialChange[] }) {
  return (
    /* A list without markers: the spine and its nodes carry the sequence
       visually, and the roles carry it for assistive technology. */
    <div className="timeline" role="list">
      {changes.map((change) => {
        const severity = severityCopy(change.severity)
        return (
          <div
            className={`change${severity.node}`}
            role="listitem"
            key={change.change_id}
          >
            <div className="row row-3 wrap">
              <span className="change__subject">{humanize(change.subject)}</span>
              <span className={`status status--${severity.tone}`}>
                {severity.label}
              </span>
            </div>
            <div className="change__kind">
              {kindLabel(change.kind)} · {humanize(change.category)}
            </div>

            {change.transitions.length === 0 ? null : (
              <div className="change__transitions">
                {change.transitions.map((transition) => (
                  <div
                    className="row row-2 wrap"
                    key={`${change.change_id}-${transition.field}`}
                  >
                    <span className="change__kind">
                      {fieldLabel(transition.field)}
                    </span>
                    <span className="tabular">
                      <TransitionValue
                        field={transition.field}
                        value={transition.before}
                        absent="Not set before"
                      />
                    </span>
                    <span className="change__arrow" aria-hidden="true">
                      →
                    </span>
                    <span className="sr-only">changed to</span>
                    <span className="tabular">
                      <TransitionValue
                        field={transition.field}
                        value={transition.after}
                        absent="Not set now"
                      />
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

/* ---------------------------------------------------------------- counts -- */

function ChangeCounts({ summary }: { summary: ChangeSummary }) {
  const severities = [
    ...SEVERITY_ORDER.filter((key) => key in summary.by_severity),
    ...Object.keys(summary.by_severity).filter(
      (key) => !SEVERITY_ORDER.includes(key),
    ),
  ]
  const categories = Object.keys(summary.by_category)
  const notable = NOTABLE.filter((item) => summary[item.key] > 0)

  return (
    <div className="stack stack-6">
      <div className="figrow figrow--3">
        <div className="figrow__cell">
          <span className="figrow__label">Changes recorded</span>
          <span className="figure figure--sm tabular">
            {plainNumber(summary.total)}
          </span>
        </div>
        {Object.entries(summary.by_kind).map(([kind, count]) => (
          <div className="figrow__cell" key={kind}>
            <span className="figrow__label">{kindLabel(kind)}</span>
            <span className="figure figure--sm tabular">{plainNumber(count)}</span>
          </div>
        ))}
      </div>

      <div className="table-scroll">
        <table className="data-table">
          <caption className="sr-only">Changes by severity</caption>
          <thead>
            <tr>
              <th scope="col">Severity</th>
              <th scope="col" className="numeric">
                Changes
              </th>
            </tr>
          </thead>
          <tbody>
            {severities.length === 0 ? (
              <tr>
                <th scope="row">None recorded</th>
                <td className="numeric">0</td>
              </tr>
            ) : (
              severities.map((key) => (
                <tr key={key}>
                  <th scope="row">{severityCopy(key).label}</th>
                  <td className="numeric tabular">
                    {plainNumber(summary.by_severity[key] ?? 0)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {categories.length > 0 ? (
        <div className="table-scroll">
          <table className="data-table">
            <caption className="sr-only">Changes by category</caption>
            <thead>
              <tr>
                <th scope="col">What moved</th>
                <th scope="col" className="numeric">
                  Changes
                </th>
              </tr>
            </thead>
            <tbody>
              {categories.map((key) => (
                <tr key={key}>
                  <th scope="row">{humanize(key)}</th>
                  <td className="numeric tabular">
                    {plainNumber(summary.by_category[key] ?? 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <div className="stack stack-3">
        <span className="eyebrow">Notable movements</span>
        {notable.length === 0 ? (
          <p className="text-sm text-secondary">
            None of the counted movements — new opportunities, urgency,
            evidence, freshness — registered anything this time.
          </p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">
                Named movements with a count above zero
              </caption>
              <thead>
                <tr>
                  <th scope="col">Movement</th>
                  <th scope="col" className="numeric">
                    Count
                  </th>
                </tr>
              </thead>
              <tbody>
                {notable.map((item) => (
                  <tr key={item.key}>
                    <th scope="row">{item.label}</th>
                    <td className="numeric tabular">
                      {plainNumber(summary[item.key])}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-muted measure">
          Counts only. Onyx does not put a figure on what a change was worth,
          because no authority here calculated one.
        </p>
      </div>
    </div>
  )
}

/* -------------------------------------------------------- AI explanation -- */

function ChangesExplanation({ taxYear }: { taxYear: number }) {
  const [asked, setAsked] = useState(false)
  const explanation = useExplanation({
    type: 'WHAT_CHANGED',
    taxYear,
    enabled: asked,
  })

  return (
    <div className="stack stack-4">
      <p className="text-sm text-secondary measure">
        Onyx can summarise this list in plain words. The events do not change —
        the wording is written from the same ones shown above.
      </p>

      <div>
        <button
          type="button"
          className="btn btn--secondary"
          onClick={() => setAsked((value) => !value)}
          aria-expanded={asked}
          aria-controls="changes-explanation"
        >
          {asked ? 'Hide explanation' : 'Explain what changed'}
        </button>
      </div>

      <div id="changes-explanation">
        {!asked ? null : explanation.isPending ? (
          <LoadingBlock label="Writing a summary of what changed" />
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

/* ------------------------------------------------------------------ page -- */

interface AcknowledgeInput {
  snapshotHash: string
  baselineCheckpointId: string | null
}

export default function WhatChanged() {
  const { taxYear } = useTaxYear()
  const changes = useChanges(taxYear)
  const queryClient = useQueryClient()

  /**
   * Acknowledge the state that was just read.
   *
   * NEVER retried. The request carries the snapshot the customer actually saw,
   * and a retry after a refusal would either replay a decision the backend
   * already declined or acknowledge a state that has since moved. A fresh
   * `request_id` is minted per attempt because each attempt is a separate,
   * deliberate act by the customer; the backend deduplicates a repeated id
   * rather than a repeated intent.
   *
   * BACKEND PLUMBING GAP: `POST /ioe/changes/acknowledge` also requires a
   * `tax_year` query parameter, and the shared `ioeApi.acknowledgeChanges`
   * helper does not send one. Until that helper is corrected the request is
   * refused by validation. Nothing is worked around here — the failure is
   * reported to the customer through the ordinary error path rather than
   * hidden or faked.
   */
  const acknowledge = useMutation({
    mutationFn: (input: AcknowledgeInput) =>
      ioeApi.acknowledgeChanges({
        snapshot_hash: input.snapshotHash,
        baseline_checkpoint_id: input.baselineCheckpointId,
        request_id: crypto.randomUUID(),
      }),
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.changes(taxYear) })
    },
    onError: (error) => {
      // 409 means the state moved while it was being read. Reloading is the
      // honest response: the customer must see the current state before they
      // can say they have reviewed it.
      if (error instanceof ApiError && error.isConflict) void changes.refetch()
    },
  })

  const failure = acknowledge.isError ? describeError(acknowledge.error) : null
  const conflicted =
    acknowledge.error instanceof ApiError && acknowledge.error.isConflict

  return (
    <>
      <PageHead
        eyebrow={`Tax year ${taxYear}`}
        title="What changed"
        lede="Everything material that has moved since the state you last acknowledged, in the order and with the weight Onyx recorded."
      />

      <div className="stack stack-6">
        {/* ------------------------------------------------ the change set */}
        <section className="panel" aria-labelledby="changes-heading">
          <div className="panel__header">
            <div>
              <h2 className="section-title" id="changes-heading">
                Since your last review
              </h2>
              <p className="text-sm text-muted mt-1">
                Band transitions, not countdowns. A deadline moving from 38 days
                to 37 is not an event; a deadline becoming urgent is.
              </p>
            </div>
          </div>

          <div className="panel__body">
            <AsyncBlock query={changes}>
              {(data) => (
                <div className="stack stack-5">
                  <div className="row row-4 wrap">
                    <span
                      className={`status status--${
                        data.baseline_status === 'ESTABLISHED' ? 'ready' : 'neutral'
                      }`}
                    >
                      {data.baseline_status === 'ESTABLISHED'
                        ? 'Baseline set'
                        : 'No baseline yet'}
                    </span>
                    <span className="text-xs text-muted">
                      Evaluated {isoDate(data.as_of)}
                    </span>
                    {data.baseline_checkpoint ? (
                      <span className="text-xs text-muted">
                        Measured from the state you reviewed on{' '}
                        {isoDate(data.baseline_checkpoint.acknowledged_at)}
                      </span>
                    ) : null}
                  </div>

                  {data.baseline_status === 'NO_BASELINE' ? (
                    /* Not an error, and not an empty result either. Nothing has
                       been acknowledged, so nothing can honestly be called new. */
                    <EmptyState
                      title="Nothing to compare against yet"
                      body="You have not acknowledged a starting state for this tax year. Onyx will not describe the position you already have as a list of things that just happened. Once you confirm you have seen where you stand, it tracks what moves from that point."
                    />
                  ) : data.changes.length === 0 ? (
                    <EmptyState
                      title="Nothing material has changed"
                      body="Nothing in your tax picture has moved since the state you last reviewed. Onyx keeps watching; there is nothing for you to read here today."
                    />
                  ) : (
                    <ChangeTimeline changes={data.changes} />
                  )}
                </div>
              )}
            </AsyncBlock>
          </div>

          {changes.data ? (
            <div className="panel__footer">
              <div className="stack stack-4">
                {failure ? (
                  <div className="error-summary" role="alert">
                    <div className="stack stack-2">
                      <strong className="text-sm">{failure.title}</strong>
                      <p className="text-sm">{failure.body}</p>
                      {conflicted ? (
                        <p className="text-sm">
                          Your position moved while this page was open, so
                          nothing was recorded. The list above has been reloaded
                          — read it again, then acknowledge it.
                        </p>
                      ) : null}
                    </div>
                  </div>
                ) : null}

                <p aria-live="polite" role="status" className="text-sm text-secondary">
                  {acknowledge.isSuccess
                    ? 'Recorded. From here on, Onyx reports what moves away from this state.'
                    : ''}
                </p>

                <p className="text-sm text-secondary measure">
                  Acknowledging records the state you have just read as your new
                  starting point. It changes nothing about your tax position,
                  and it is the only thing that moves the line Onyx measures
                  from.
                </p>

                <div>
                  <button
                    type="button"
                    className="btn btn--primary"
                    disabled={acknowledge.isPending || changes.isFetching}
                    onClick={() => {
                      const data = changes.data
                      if (!data) return
                      acknowledge.mutate({
                        snapshotHash: data.current_snapshot_hash,
                        baselineCheckpointId: data.baseline_checkpoint?.id ?? null,
                      })
                    }}
                  >
                    {acknowledge.isPending ? 'Recording…' : 'I have reviewed this'}
                  </button>
                </div>
              </div>
            </div>
          ) : null}
        </section>

        {/* ------------------------------------------------------- counts */}
        <section className="panel" aria-labelledby="counts-heading">
          <div className="panel__header">
            <div>
              <h2 className="section-title" id="counts-heading">
                The counts
              </h2>
              <p className="text-sm text-muted mt-1">
                Transparent totals over the same change set.
              </p>
            </div>
          </div>
          <div className="panel__body">
            <AsyncBlock query={changes}>
              {(data) => <ChangeCounts summary={data.summary} />}
            </AsyncBlock>
          </div>
          {changes.data ? (
            <div className="panel__footer">
              <p className="text-xs text-muted measure">
                Change set format {changes.data.schema_version}
              </p>
            </div>
          ) : null}
        </section>

        {/* -------------------------------------------------- AI wording */}
        <section className="panel" aria-labelledby="explain-heading">
          <div className="panel__header">
            <h2 className="section-title" id="explain-heading">
              In words
            </h2>
          </div>
          <div className="panel__body">
            <ChangesExplanation taxYear={taxYear} />
          </div>
        </section>
      </div>
    </>
  )
}
