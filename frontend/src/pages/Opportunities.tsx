/* =========================================================================
   OPPORTUNITIES  —  the queue, and the record behind each item
   =========================================================================
   One file, two views, chosen by the route parameter:

     /app/opportunities             the queue, in the backend's review order
     /app/opportunities/:sourceId   the full record for one item

   Three commitments shape everything below.

   NOTHING IS RANKED HERE. The order of the queue is `assurance.attention`,
   which the Assurance module documents as a presentation order with a
   published key — urgency band, then closable-gap band, then the optimizer's
   own sealed rank. Re-sorting by dollar value in the browser would turn a
   review order into a second recommendation engine, so this file only maps
   ids back to records and renders them in the order it was given.

   NOTHING IS HIDDEN. An exclusion is a result. Blocked, ineligible and
   indeterminate items appear in the same list as the rest, with the governed
   reason spelled out, because a list that quietly drops what did not qualify
   teaches a customer that absence means nothing was considered.

   NOTHING IS MERGED. Eligibility, assurance status, next action, urgency,
   evidence readiness, freshness and integrity answer different questions.
   They stay in separate facets even where that costs space, and the lifecycle
   panel keeps its seven axes apart for the same reason.
   ========================================================================= */
import { useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PageHead, useTaxYear } from '@/components/Shell'
import {
  AsyncBlock,
  EmptyState,
  ErrorState,
  LoadingBlock,
} from '@/components/states'
import {
  AiExplanation,
  EligibilityBadge,
  Provenance,
  SupportSignal,
  TrustPair,
} from '@/components/trust'
import { humanize, isoDate, money, relativeDays } from '@/lib/format'
import { level1, level2, technical } from '@/lib/lexicon'
import { DetailList, Disclosure } from '@/components/disclosure'
import { useExplanation } from '@/lib/explanation'
import { useAssurance, useLifecycle } from '@/lib/queries'
import type { OpportunityLifecycleOut, TaxAssuranceOut } from '@/api/endpoints'

type Opportunity = TaxAssuranceOut['opportunities'][number]
type LifecycleItem = OpportunityLifecycleOut['opportunities'][number]

/* =========================================================================
   VOCABULARY
   -------------------------------------------------------------------------
   The backend's closed enumerations, given wording a person can read. Every
   map falls through to its most cautious member, never to its most
   affirmative one: an unrecognised value must not become "ready", "available"
   or "no deadline" on the strength of the frontend not knowing it.
   ========================================================================= */

/** Eligibility drives the record's left edge. The edge is decoration — the
 *  badge beside it carries the same verdict in words. */
const EDGE: Record<string, string> = {
  eligible: 'opp--eligible',
  conditionally_eligible: 'opp--conditional',
  ineligible: 'opp--ineligible',
  indeterminate: 'opp--indeterminate',
}

interface Tone {
  tone: string
  label: string
}

/* STROKE AND BADGE TONE ONLY.
   These tables used to carry WORDS as well, which made them a second consumer
   vocabulary sitting beside the lexicon and disagreeing with it in small ways
   — "Evidence is needed" here, "We need a document" there, for the same
   governed state. The words now come from one place. What stays here is the
   class name that decides how a badge is drawn, which is presentation and
   belongs to the page. */
const STATUS_TONE: Record<string, string> = {
  READY: 'ready',
  EVIDENCE_REQUIRED: 'attention',
  REVIEW_REQUIRED: 'attention',
  BLOCKED: 'blocked',
  UNAVAILABLE: 'neutral',
  NOT_APPLICABLE: 'neutral',
}

const ACTION_TONE: Record<string, string> = {
  ACTION_AVAILABLE: 'ready',
  DECISION_REQUIRED: 'attention',
  EVIDENCE_REQUIRED: 'attention',
  BLOCKED: 'blocked',
}

const URGENCY_TONE: Record<string, string> = {
  NO_DEADLINE: 'neutral',
  NORMAL: 'neutral',
  APPROACHING: 'attention',
  URGENT: 'attention',
  EXPIRED: 'blocked',
}

/** Whether the documents a governed rule demands are held. Deliberately not
 *  folded into the item's status: a document gap and a rule exclusion are
 *  different problems with different fixes. */
const READINESS_TONE: Record<string, string> = {
  READY: 'ready',
  PARTIAL: 'attention',
  MISSING: 'attention',
  NOT_REQUIRED: 'neutral',
  UNKNOWN: 'neutral',
}

/** An unrecognised code must not become "no deadline" or "ready" — that would
 *  be a claim about the item rather than an admission about the label. The
 *  lexicon returns null for anything it has no words for, and this says so
 *  rather than prettifying the code. */
const UNDETERMINED = 'Not determined'

function toned(
  table: Record<string, string>,
  vocabulary: Parameters<typeof level1>[0],
  code: string,
  cautious: string,
): Tone {
  return {
    tone: table[code] ?? cautious,
    label: level1(vocabulary, code) ?? technical(vocabulary, code) ?? UNDETERMINED,
  }
}

function statusTone(code: string): Tone {
  return toned(STATUS_TONE, 'assuranceStatus', code, 'neutral')
}

function actionTone(code: string): Tone {
  return toned(ACTION_TONE, 'action', code, 'blocked')
}

function urgencyTone(code: string): Tone {
  return toned(URGENCY_TONE, 'urgency', code, 'neutral')
}

function readinessTone(code: string): Tone {
  return toned(READINESS_TONE, 'evidenceReadiness', code, 'neutral')
}

function edgeClass(eligibility: string): string {
  return EDGE[eligibility] ?? EDGE['indeterminate']!
}

/** The user's own report of having acted. Never system verification — no
 *  governed source can prove a contribution happened, and the wording here
 *  must not suggest one did. */
const EXECUTION: Record<string, string> = {
  NOT_REPORTED: 'You have not reported acting',
  USER_REPORTED: 'You reported acting',
}

/* =========================================================================
   SHARED PIECES
   ========================================================================= */

function Facet({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <span className="opp__facet-label">{label}</span>
      <div className="stack stack-2">{children}</div>
    </div>
  )
}

/** Governed reason codes as readable sentences. The codes themselves are
 *  never shown — they are machine tokens, and `humanize` is a label
 *  transform, not a translation of what the code decided. */
function ReasonList({ codes }: { codes: string[] }) {
  /* A code with no consumer wording is DROPPED, not prettified. Showing
     nothing is honest; showing a reformatted identifier is a sentence nobody
     wrote and nobody reviewed. The exhaustiveness guard is what stops this
     from quietly hiding a state that should have been translated. */
  const said = codes
    .map((code) => ({ code, text: level1('reason', code) }))
    .filter((entry): entry is { code: string; text: string } => entry.text !== null)
  if (said.length === 0) return null
  return (
    <ul className="stack stack-2 pl-5">
      {said.map((entry) => (
        <li className="text-sm text-secondary" key={entry.code}>
          {entry.text}
        </li>
      ))}
    </ul>
  )
}

/**
 * Why an item cannot proceed, in the backend's own words.
 *
 * Blocked and stale are separate statements and are printed separately: a
 * governed exclusion means the rules say no, while stale inputs mean the
 * verdict was reached before something changed. Collapsing them would hide
 * the fact that a re-run might resolve one and never the other.
 */
function Impediments({
  item,
  explain = false,
}: {
  item: Opportunity
  /** The queue states the impediment; the record explains it. Repeating the
   *  full explanation on every row of a long list buries the list. */
  explain?: boolean
}) {
  const blocked = item.blocked_reason_code
  const stale = item.stale_reason_codes ?? []
  if (!blocked && stale.length === 0) return null

  return (
    <div className="stack stack-3">
      {blocked ? (
        <div className="stack stack-2">
          <div className="row row-2 wrap">
            {/* Both callers already show the status badge, which says
                "Blocked". Repeating the word here spent a second chip saying
                nothing new and left five equal-weight labels competing on one
                card. What the reader still needs is WHY. */}
            <span className="eyebrow">Why it is blocked</span>
            <span className="text-sm text-secondary">
              {level1('reason', blocked) ?? technical('reason', blocked) ?? UNDETERMINED}
            </span>
          </div>
          {explain ? (
            <p className="text-sm text-muted measure">
              A governed exclusion applies, so this cannot be acted on as things
              stand. It is shown rather than removed, because knowing something
              was considered and ruled out is a result.
            </p>
          ) : null}
        </div>
      ) : null}

      {stale.length > 0 ? (
        <div className="stack stack-2">
          <div className="row row-2 wrap">
            <span className="status status--attention">May be out of date</span>
            <span className="text-sm text-secondary">
              {stale
                .map((code) => level1('reason', code))
                .filter((text): text is string => text !== null)
                .join('. ')}
            </span>
          </div>
          {explain ? (
            <p className="text-sm text-muted measure">
              Something changed after this verdict was produced. Re-run your
              analysis to get a statement about today.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

/* =========================================================================
   THE QUEUE
   ========================================================================= */

/** One record in the queue. A ruled entry, not a card in a grid: the left
 *  edge states the eligibility band and the badge states it again in words,
 *  so nothing depends on seeing colour. */
function QueueRecord({ item }: { item: Opportunity }) {
  const status = statusTone(item.status)
  const action = actionTone(item.action)
  const urgency = urgencyTone(item.urgency)
  /* The finding's NAME. Was `humanize(item.opportunity_code)`, which rendered
     INCREASE_RRSP_DEDUCTION as "Increase rrsp deduction" — a code, reformatted,
     with a proper name case-folded. */
  const title =
    level1('opportunity', item.opportunity_code) ??
    technical('opportunity', item.opportunity_code) ??
    'Something worth checking'
  const headingId = `opp-${item.source_id}`

  return (
    <article
      className={`opp ${edgeClass(item.eligibility_status)}`}
      aria-labelledby={headingId}
    >
      <div className="opp__head">
        <h3 className="opp__title" id={headingId}>
          {title}
        </h3>
        <div className="row row-2 wrap">
          <EligibilityBadge status={item.eligibility_status} />
          <span className={`status status--${status.tone}`}>{status.label}</span>
        </div>
      </div>

      <div className="stack stack-3">
        {/* LEVEL 1 AND THE ACTION STAY TOGETHER, ABOVE EVERY DISCLOSURE.
            What needs doing, whether time is short, and the way to act on it
            are the three things a first-time filer must see without opening
            anything. Progressive disclosure is for explanation; burying the
            task behind it is the failure this pattern exists to avoid. */}
        <Disclosure
          about={title}
          action={
            <Link
              className="btn btn--secondary"
              to={`/app/opportunities/${encodeURIComponent(item.source_id)}`}
            >
              Open the record
              <span className="sr-only"> for {title}</span>
            </Link>
          }
          level1={
            <div className="stack stack-2">
              <div className="row row-3 wrap">
                <span className={`status status--${action.tone}`}>{action.label}</span>
                <span className={`status status--${urgency.tone}`}>{urgency.label}</span>
                {item.assumption_dependent ? (
                  <Provenance kind="assumption" label="Depends on an assumption" />
                ) : null}
              </div>
              {item.deadline ? (
                <p className="text-sm text-secondary">
                  {isoDate(item.deadline.deadline_date)},{' '}
                  {relativeDays(item.deadline.days_remaining)}.
                </p>
              ) : null}
            </div>
          }
          level2={level2('opportunity', item.opportunity_code)}
          detail={
            /* LEVEL 3 — supporting state the contract already carries. Nothing
               here is computed at render time and nothing is inferred. */
            <DetailList
              items={[
                { label: 'Where this stands', value: status.label },
                { label: 'What Onyx needs', value: action.label },
                { label: 'Documents', value: readinessTone(item.evidence_readiness).label },
                { label: 'Deadline', value: urgency.label },
                {
                  label: 'Depends on an assumption',
                  value: item.assumption_dependent ? 'Yes' : 'No',
                },
              ]}
            />
          }
          technical={
            /* LEVEL 4 — the formal register. The internal codes are NOT
               reproduced raw; each is the lexicon's approved technical term,
               which is the name a person auditing this would use. `source_id`
               is here because it is the handle support and the customer would
               both quote; nothing else identifier-shaped is. */
            <DetailList
              items={[
                { label: 'Formal name', value: technical('opportunity', item.opportunity_code) },
                { label: 'Assurance status', value: technical('assuranceStatus', item.status) },
                { label: 'Action state', value: technical('action', item.action) },
                { label: 'Deadline band', value: technical('urgency', item.urgency) },
                {
                  label: 'Evidence readiness',
                  value: technical('evidenceReadiness', item.evidence_readiness),
                },
                { label: 'Record reference', value: item.source_id },
              ]}
            />
          }
        />

        <Impediments item={item} />
      </div>
    </article>
  )
}

/**
 * The queue, in the order the backend published.
 *
 * `attention` is the order; `opportunities` is the set. Anything in the set
 * that the order does not name is appended rather than dropped — the two
 * agree today, and the day they stop agreeing an item must not silently
 * vanish from a customer's list.
 */
function orderedQueue(assurance: TaxAssuranceOut): Opportunity[] {
  const byId = new Map(assurance.opportunities.map((item) => [item.source_id, item]))
  const ordered: Opportunity[] = []
  const seen = new Set<string>()

  for (const sourceId of assurance.attention ?? []) {
    const item = byId.get(sourceId)
    if (item && !seen.has(sourceId)) {
      ordered.push(item)
      seen.add(sourceId)
    }
  }
  for (const item of assurance.opportunities) {
    if (seen.has(item.source_id)) continue
    ordered.push(item)
    seen.add(item.source_id)
  }
  return ordered
}

function QueueBody({ assurance }: { assurance: TaxAssuranceOut }) {
  const items = orderedQueue(assurance)

  if (items.length === 0) {
    /* An empty list has two very different meanings, and the OPPORTUNITY
       family is what tells them apart: a run that found nothing, or no run at
       all. Onyx never reports an unasked question as a clean result. */
    const family = assurance.families.find((entry) => entry.family === 'OPPORTUNITY')
    const established = family?.status === 'READY'
    return (
      <div className="stack stack-3">
        <EmptyState
          title={
            established
              ? 'No opportunities were found for this year'
              : 'Nothing has been established for this year yet'
          }
          body={
            established
              ? 'The governing run completed and produced no opportunities for this tax year. That is a result, not a gap.'
              : 'No governing run has produced opportunities for this tax year, so this list is empty because the question has not been answered — not because Onyx looked and found nothing.'
          }
          action={
            established ? undefined : (
              <Link className="btn btn--primary" to="/app">
                Go to your overview
              </Link>
            )
          }
        />
        {family ? (
          <p className="text-xs text-muted">
            Opportunity family: {statusTone(family.status).label}.{' '}
            {level1('reason', family.reason_code) ??
              technical('reason', family.reason_code)}.
          </p>
        ) : null}
      </div>
    )
  }

  return (
    <div className="stack stack-4">
      {items.map((item) => (
        <QueueRecord item={item} key={item.source_id} />
      ))}
    </div>
  )
}

/* =========================================================================
   THE RECORD
   ========================================================================= */

function EstimatedEffect({ item }: { item: Opportunity }) {
  const hasFigure =
    item.standalone_potential !== null && item.standalone_potential !== undefined
  const hasIncremental =
    item.incremental_portfolio_benefit !== null &&
    item.incremental_portfolio_benefit !== undefined

  if (!hasFigure && !hasIncremental) {
    return (
      <p className="text-sm text-secondary">
        No impact figure was sealed for this item, so Onyx has none to show.
      </p>
    )
  }

  return (
    <div className="stack stack-3">
      <div className="figrow">
        <div className="figrow__cell">
          <span className="figrow__label">On its own</span>
          <span className="figure figure--sm tabular">
            {money(item.standalone_potential)}
          </span>
        </div>
        <div className="figrow__cell">
          <span className="figrow__label">Added to the rest</span>
          <span className="figure figure--sm tabular">
            {money(item.incremental_portfolio_benefit)}
          </span>
        </div>
      </div>
      <Provenance kind="calculated" />
      <p className="text-xs text-muted measure-58">
        Sealed by the run that produced this opportunity. The first figure is
        this item on its own; the second is what it adds once the rest of your
        plan is already counted. They measure different things, so Onyx does
        not add them together.
      </p>
    </div>
  )
}

function EvidenceFacet({ item }: { item: Opportunity }) {
  const readiness = readinessTone(item.evidence_readiness)
  const requirements = item.evidence_requirements ?? []

  return (
    <div className="stack stack-3">
      <span className={`status status--${readiness.tone}`}>{readiness.label}</span>
      {requirements.length === 0 ? (
        <p className="text-sm text-secondary">
          No governed document requirement is recorded for this opportunity.
        </p>
      ) : (
        <div>
          {requirements.map((requirement) => {
            const state = readinessTone(requirement.readiness)
            return (
              <div
                className="evidence-row"
                key={`${requirement.document_type_code}-${requirement.necessity}`}
              >
                <span>
                  <span className="evidence-row__name">
                    {humanize(requirement.document_type_code)}
                  </span>
                  {/* SAFE PRESENTATION, NOT A GOVERNED STATE. `necessity` is
                      rule-authored free text on a `Text` column ("required",
                      "conditional"), not a closed enum, so it cannot be mapped
                      exhaustively and generic humanisation is appropriate.
                      The previous entry looked it up against `evidenceReadiness`,
                      a DIFFERENT vocabulary — the lookup never matched and
                      humanize() silently did all the work, which reads like a
                      translation and is not one. */}
                  <span className="evidence-row__necessity">
                    {humanize(requirement.necessity)}
                  </span>
                </span>
                <span className={`status status--${state.tone}`}>
                  {state.label}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function DeadlineFacet({ item }: { item: Opportunity }) {
  const deadline = item.deadline

  /* Absence of a governed deadline row is absence of a deadline. A filing
     date is never assumed here, however obvious one might seem. */
  if (!deadline) {
    return (
      <p className="text-sm text-secondary">
        No governed deadline is recorded for this opportunity. Onyx does not
        supply one of its own.
      </p>
    )
  }

  const urgency = urgencyTone(deadline.urgency)
  return (
    <div className="stack stack-2">
      <span className="text-sm">{humanize(deadline.deadline_code)}</span>
      <span className="figure figure--sm tabular">
        {isoDate(deadline.deadline_date)}
      </span>
      <div className="row row-2 wrap">
        <span className={`status status--${urgency.tone}`}>{urgency.label}</span>
        <span className="text-sm text-secondary">
          Falls {relativeDays(deadline.days_remaining)}
        </span>
      </div>
      <p className="text-xs text-muted">
        {deadline.is_hard
          ? 'The governed source marks this date as fixed.'
          : 'The governed source does not mark this date as fixed.'}
        {item.deadline_count > 1
          ? ` Onyx counted ${item.deadline_count} governed deadlines for this item and shows the earliest.`
          : ''}
      </p>
    </div>
  )
}

/** Every facet the assurance record carries, in the order a person asks
 *  about them: what, why, how much, what next, what it costs, what it needs,
 *  what it assumes, when. */
function RecordFacets({ item }: { item: Opportunity }) {
  const status = statusTone(item.status)
  const action = actionTone(item.action)
  const reviewReasons = item.review_reason_codes ?? []

  return (
    <div className="stack stack-4">
      <div className="row row-2 wrap">
        <EligibilityBadge status={item.eligibility_status} />
        <span className={`status status--${status.tone}`}>{status.label}</span>
        {item.assumption_dependent ? (
          <Provenance kind="assumption" label="Depends on an assumption" />
        ) : null}
      </div>

      <Impediments item={item} explain />

      <div className="opp__grid">
        <Facet label="What it is">
          <span className="text-sm">
            {level1('opportunity', item.opportunity_code) ??
              technical('opportunity', item.opportunity_code) ??
              'Something worth checking'}
          </span>
          <p className="text-xs text-muted">
            Named by the governed rule that produced it.
          </p>
        </Facet>

        <Facet label="Why it may apply">
          <EligibilityBadge status={item.eligibility_status} />
          {reviewReasons.length > 0 ? (
            <>
              <p className="text-sm text-secondary">
                Conditions the governed rules recorded against this item:
              </p>
              <ReasonList codes={reviewReasons} />
            </>
          ) : (
            <p className="text-sm text-secondary">
              No review condition is recorded against this item.
            </p>
          )}
        </Facet>

        <Facet label="Estimated effect">
          <EstimatedEffect item={item} />
        </Facet>

        <Facet label="Action">
          <span className={`status status--${action.tone}`}>{action.label}</span>
          <p className="text-xs text-muted">
            What the governed authorities say the next move is. It is not a
            recommendation to act.
          </p>
        </Facet>

        <Facet label="Required cash or resource">
          {/* The assurance record carries no cash or resource commitment for
              an individual opportunity. Rather than infer one from the impact
              figures — which measure something else entirely — this facet says
              so. See the contract note in the summary. */}
          <p className="text-sm text-secondary">
            This record does not state what acting on this opportunity would
            require you to have available. Onyx will not estimate it from the
            figures above, because those measure the effect on your tax, not the
            money you would need to commit. Where the governed input records a
            commitment, the explanation at the foot of this page describes it.
          </p>
        </Facet>

        <Facet label="Evidence needed">
          <EvidenceFacet item={item} />
        </Facet>

        <Facet label="Assumptions">
          {item.assumption_dependent ? (
            <>
              <Provenance kind="assumption" />
              <p className="text-sm text-secondary">
                The governed support model adjusted this item for assumption
                uncertainty: at least one value behind it was assumed rather
                than confirmed. Treat the figures as modelled, and confirm the
                underlying amounts before acting on them.
              </p>
            </>
          ) : (
            <p className="text-sm text-secondary">
              The support model did not adjust this item for assumption
              uncertainty.
            </p>
          )}
        </Facet>

        <Facet label="Deadline">
          <DeadlineFacet item={item} />
        </Facet>
      </div>
    </div>
  )
}

/* =========================================================================
   LIFECYCLE — seven axes, kept apart
   ========================================================================= */

/**
 * Five of the seven axes.
 *
 * Freshness and integrity are the other two, and the lifecycle view carries
 * them verbatim from the same assurance item shown above. Printing them again
 * here would state the same two facts twice in two places, so this panel names
 * where they live instead of repeating them.
 */
function LifecycleAxes({ item }: { item: LifecycleItem }) {
  const rows: { axis: string; value: string; note: string }[] = [
    {
      axis: 'Availability',
      value: humanize(item.availability),
      note: 'What the governed rules and exclusions say.',
    },
    {
      axis: 'Your decision',
      value: humanize(item.decision),
      note: 'What you recorded in your decision journal.',
    },
    {
      axis: 'Reported action',
      value: EXECUTION[item.execution] ?? humanize(item.execution),
      note: 'Your own report. Onyx does not verify that anything happened.',
    },
    {
      axis: 'Evidence',
      value: readinessTone(item.evidence).label,
      note: 'Whether the documents the rule demands are held.',
    },
    {
      axis: 'Timing',
      value: urgencyTone(item.timing).label,
      note: 'Measured against the governed deadline, where there is one.',
    },
  ]

  return (
    <div className="stack stack-4">
      <div className="row row-2 wrap">
        <span className="text-sm text-secondary">Next move:</span>
        <span className="text-sm">{humanize(item.actionability)}</span>
      </div>

      <div className="table-scroll">
        <table className="data-table">
          <caption className="sr-only">
            Five of the seven lifecycle axes for this opportunity. The other
            two, freshness and integrity, are shown with the record above.
          </caption>
          <thead>
            <tr>
              <th scope="col">Axis</th>
              <th scope="col">Where it stands</th>
              <th scope="col">What the axis answers</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.axis}>
                <th scope="row">{row.axis}</th>
                <td>{row.value}</td>
                <td className="text-muted">{row.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {item.reason_codes.length > 0 ? (
        <div className="stack stack-2">
          <span className="eyebrow">Why it stands here</span>
          <ReasonList codes={item.reason_codes} />
        </div>
      ) : null}

      {item.journal ? (
        <p className="text-sm text-secondary">
          {item.journal.thread_count === 1
            ? 'You have one decision thread about this opportunity.'
            : `You have ${item.journal.thread_count} decision threads about this opportunity. Onyx reports the most recent one above.`}
          {item.last_reported_action_date
            ? ` You reported acting on ${isoDate(item.last_reported_action_date)}.`
            : ''}
        </p>
      ) : null}

      <p className="text-xs text-muted measure">
        These axes are kept apart on purpose. An opportunity can be urgent and
        already acted on, or expired and still worth recording, and a single
        combined status could not say either. The remaining two axes — whether
        this is still current, and whether it reproduces — describe the result
        rather than your progress, and are shown with the record above.
      </p>
    </div>
  )
}

/* =========================================================================
   AI EXPLANATION — on demand only
   ========================================================================= */

/**
 * Asked for, never volunteered: an explanation is admission-controlled work
 * charged to the customer's own budget, and pre-fetching one for a panel
 * nobody opened spends it for nothing.
 *
 * `what_you_can_do` and `what_you_need` are rendered here as children because
 * they are structured lists the shared component leaves to the surface. They
 * are still model output and still plain React children — there is no HTML
 * path anywhere in this file.
 */
function OpportunityExplanation({
  sourceId,
  taxYear,
}: {
  sourceId: string
  taxYear: number
}) {
  const [asked, setAsked] = useState(false)
  const explanation = useExplanation({
    type: 'OPPORTUNITY',
    subjectId: sourceId,
    taxYear,
    enabled: asked,
  })

  return (
    <div className="stack stack-4">
      <p className="text-sm text-secondary measure">
        Onyx can put this record into words: why it may apply, what it would
        take, and what it depends on. Nothing about the figures changes — the
        wording is written from the same ones shown above.
      </p>

      <div>
        <button
          type="button"
          className="btn btn--secondary"
          onClick={() => setAsked((value) => !value)}
          aria-expanded={asked}
          aria-controls="opportunity-explanation"
        >
          {asked ? 'Hide explanation' : 'Explain this opportunity'}
        </button>
      </div>

      <div id="opportunity-explanation">
        {!asked ? null : explanation.isPending ? (
          <LoadingBlock label="Writing an explanation of this opportunity" />
        ) : explanation.isError ? (
          <ErrorState
            error={explanation.error}
            onRetry={() => void explanation.refetch()}
          />
        ) : (
          <AiExplanation explanation={explanation.data.explanation}>
            <ExplanationLists explanation={explanation.data.explanation} />
          </AiExplanation>
        )}
      </div>
    </div>
  )
}

function ExplanationLists({
  explanation,
}: {
  explanation: {
    what_you_can_do?: { action_ref: string; description: string }[]
    what_you_need?: string[]
  }
}) {
  const steps = explanation.what_you_can_do ?? []
  const needs = explanation.what_you_need ?? []
  if (steps.length === 0 && needs.length === 0) return null

  return (
    <>
      {steps.length > 0 ? (
        <div className="ai-block__section">
          <div className="ai-block__heading">What you can do</div>
          <ul className="stack stack-2 pl-5">
            {steps.map((step) => (
              <li key={step.action_ref}>{step.description}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {needs.length > 0 ? (
        <div className="ai-block__section">
          <div className="ai-block__heading">What you need</div>
          <ul className="stack stack-2 pl-5">
            {needs.map((need) => (
              <li key={need}>{need}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  )
}

/* =========================================================================
   VIEWS
   ========================================================================= */

function OpportunityQueue({ taxYear }: { taxYear: number }) {
  const assurance = useAssurance(taxYear)

  return (
    <>
      <PageHead
        eyebrow={`Tax year ${taxYear}`}
        title="Opportunities"
        lede="Everything the governed rules considered for this year, including what they ruled out."
      />

      <section className="panel" aria-labelledby="queue-heading">
        <div className="panel__header">
          <div>
            <h2 className="section-title" id="queue-heading">
              In review order
            </h2>
            <p className="text-sm text-muted mt-1">
              Onyx lists these in the order it suggests reviewing them: time
              pressure first, then gaps you can close. That is a reading order,
              not a ranking by value.
            </p>
          </div>
        </div>
        <div className="panel__body">
          <AsyncBlock query={assurance}>
            {(data) => <QueueBody assurance={data} />}
          </AsyncBlock>
        </div>
        {assurance.data ? (
          <div className="panel__footer">
            <p className="text-xs text-muted measure">
              {assurance.data.summary.opportunity_count}{' '}
              {assurance.data.summary.opportunity_count === 1
                ? 'opportunity was'
                : 'opportunities were'}{' '}
              considered for {taxYear}, as of{' '}
              {isoDate(assurance.data.as_of)}. Items that were excluded stay on
              this list: an exclusion is a result, and hiding it would make the
              list look like nobody checked.
            </p>
          </div>
        ) : null}
      </section>
    </>
  )
}

function OpportunityRecord({
  sourceId,
  taxYear,
}: {
  sourceId: string
  taxYear: number
}) {
  const assurance = useAssurance(taxYear)
  const lifecycle = useLifecycle(taxYear)

  const item =
    assurance.data?.opportunities.find(
      (candidate) => candidate.source_id === sourceId,
    ) ?? null
  const lifecycleItem =
    lifecycle.data?.opportunities.find(
      (candidate) => candidate.source_id === sourceId,
    ) ?? null

  const missing = (
    <EmptyState
      title="This opportunity is not in this tax year"
      body="Onyx has no record with this reference for the year currently selected. It may belong to another tax year, or the run that produced it may have been replaced by a newer one."
      action={
        <Link className="btn btn--secondary" to="/app/opportunities">
          Back to all opportunities
        </Link>
      }
    />
  )

  return (
    <>
      <PageHead
        eyebrow={`Tax year ${taxYear} · Opportunity`}
        title={
          (item &&
            (level1('opportunity', item.opportunity_code) ??
              technical('opportunity', item.opportunity_code))) ||
          'Opportunity'
        }
        lede={
          item
            ? 'The full governed record: what it is, why it may apply, and what stands between you and it.'
            : undefined
        }
        actions={
          <Link className="btn btn--ghost" to="/app/opportunities">
            All opportunities
          </Link>
        }
      />

      <div className="stack stack-6">
        {/* ----------------------------------------------------- the record */}
        <section className="panel" aria-labelledby="record-heading">
          <div className="panel__header">
            <h2 className="section-title" id="record-heading">
              The record
            </h2>
          </div>
          <div className="panel__body">
            <AsyncBlock query={assurance}>
              {() => (item ? <RecordFacets item={item} /> : missing)}
            </AsyncBlock>
          </div>
          {item ? (
            <div className="panel__footer">
              {/* Two different questions, never merged into one tick: whether
                  this is still true, and whether it re-ran to the same
                  identity. */}
              <TrustPair
                freshness={item.freshness}
                integrity={item.integrity}
              />
            </div>
          ) : null}
        </section>

        {item ? (
          <>
            {/* --------------------------------------------------- support */}
            <section className="panel" aria-labelledby="support-heading">
              <div className="panel__header">
                <h2 className="section-title" id="support-heading">
                  How well supported
                </h2>
              </div>
              <div className="panel__body">
                {item.support.display_support_score === null ||
                item.support.display_support_score === undefined ? (
                  <EmptyState
                    title="No support score was sealed for this item"
                    body="The run that produced this opportunity did not record a support score, so Onyx has nothing to report on how well it is backed."
                  />
                ) : (
                  <div className="stack stack-4">
                    <SupportSignal support={item.support} />
                    <p className="text-xs text-muted measure">
                      {item.support.disclaimer}
                    </p>
                  </div>
                )}
              </div>
            </section>

            {/* ------------------------------------------------- lifecycle */}
            <section className="panel" aria-labelledby="lifecycle-heading">
              <div className="panel__header">
                <div>
                  <h2 className="section-title" id="lifecycle-heading">
                    Where this stands
                  </h2>
                  <p
                    className="text-sm text-muted mt-1"
                  >
                    Seven independent axes, none of them derived from another.
                    Five are below; freshness and integrity sit with the record
                    above.
                  </p>
                </div>
              </div>
              <div className="panel__body">
                <AsyncBlock query={lifecycle}>
                  {(data) =>
                    lifecycleItem ? (
                      <LifecycleAxes item={lifecycleItem} />
                    ) : (
                      <EmptyState
                        title="No lifecycle record for this item"
                        body={
                          data.opportunity_authority === 'READY'
                            ? 'The lifecycle view does not currently carry this opportunity. It may have been replaced by a newer run.'
                            : `The lifecycle view has no governing run to read for this year. ${
                                level1('reason', data.opportunity_authority_reason) ?? ''
                              }`.trim()
                        }
                      />
                    )
                  }
                </AsyncBlock>
              </div>
            </section>

            {/* ---------------------------------------------------- source */}
            <section className="panel" aria-labelledby="source-heading">
              <div className="panel__header">
                <h2 className="section-title" id="source-heading">
                  Governing source
                </h2>
              </div>
              <div className="panel__body">
                {/* Onyx holds the citation text and title for the rule behind
                    this opportunity, but this response does not carry them, so
                    there is nothing to quote. A citation assembled here — or a
                    link guessed at from a rule code — would be Onyx vouching
                    for wording it was never given. */}
                <EmptyState
                  title="The source text is not available on this surface"
                  body="This record names the governed rule that produced it, but it does not carry that rule's citation text. Onyx will not quote a source it was not given, and it will not construct a link to an outside page on your behalf. Ask for the explanation below, which is written only from governed material."
                />
              </div>
            </section>

            {/* -------------------------------------------- plain language */}
            <section className="panel" aria-labelledby="explain-heading">
              <div className="panel__header">
                <h2 className="section-title" id="explain-heading">
                  In plain language
                </h2>
              </div>
              <div className="panel__body">
                <OpportunityExplanation
                  sourceId={item.source_id}
                  taxYear={taxYear}
                />
              </div>
            </section>
          </>
        ) : null}
      </div>
    </>
  )
}

/* =========================================================================
   THE PAGE
   ========================================================================= */

export default function Opportunities() {
  const { taxYear } = useTaxYear()
  const { sourceId } = useParams<{ sourceId: string }>()

  return sourceId ? (
    <OpportunityRecord sourceId={sourceId} taxYear={taxYear} key={sourceId} />
  ) : (
    <OpportunityQueue taxYear={taxYear} />
  )
}
