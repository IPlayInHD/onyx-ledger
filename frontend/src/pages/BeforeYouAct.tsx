/* =========================================================================
   BEFORE YOU ACT  —  what this decision would change
   =========================================================================
   The screen a customer reads BEFORE doing something they cannot undo. It is
   ordered like a sentence: where you stand now, what you are proposing, and
   what the sealed comparison says would be different — family by family.

   Two things here are load-bearing and easy to quietly get wrong:

     an absent delta         The comparison emits a difference only for a
                             governed numeric field whose two sides share a
                             declared unit. Its absence means the domain
                             defines no difference for that field — NOT that
                             the difference is zero. It renders as an em dash,
                             and nothing on this page ever fills one in.

     an inapplicable family  "Nothing changed in your resources" and
                             "resources are not a single-decision concept" are
                             different statements. `family_applicability` is
                             what keeps them apart, so a family that does not
                             apply is drawn differently from one that is
                             comparable and simply holds nothing.

   `comparison_hash` is deliberately never rendered: it is the engine's
   identity for the comparison, not something a customer can use.
   ========================================================================= */
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PageHead } from '@/components/Shell'
import {
  AsyncBlock,
  EmptyState,
  ErrorState,
  LoadingBlock,
} from '@/components/states'
import { AiExplanation, Provenance, TrustPair } from '@/components/trust'
import { useExplanation } from '@/lib/explanation'
import { humanize, isoDate, money, plainNumber } from '@/lib/format'
import { useComparison, useScenario } from '@/lib/queries'
import type { BeforeYouActComparisonOut, ScenarioDetailOut } from '@/api/endpoints'

type ChangeRecord = BeforeYouActComparisonOut['tax_state_changes'][number]
type FieldChange = NonNullable<ChangeRecord['fields']>[number]
type Applicability = BeforeYouActComparisonOut['family_applicability'][number]

/* ------------------------------------------------------------- vocabulary -- */

/** The seven families this contract returns records for, each with the reader
 *  that fetches its bucket. Written as accessors rather than key names so the
 *  response type stays checked rather than cast. */
const FAMILY_BUCKETS: Record<
  string,
  (comparison: BeforeYouActComparisonOut) => ChangeRecord[]
> = {
  TAX_STATE: (c) => c.tax_state_changes,
  OPPORTUNITY: (c) => c.opportunity_changes,
  EVIDENCE: (c) => c.evidence_changes,
  DEADLINE: (c) => c.deadline_changes,
  FACT: (c) => c.fact_changes,
  ASSUMPTION: (c) => c.assumption_changes,
  SCENARIO: (c) => c.scenario_changes,
}

/** Plain names for the graph's families. A label transform only — the code the
 *  backend sent is never altered, and an unrecognised family still renders. */
const FAMILY_LABELS: Record<string, string> = {
  TAX_STATE: 'Your tax position',
  OPPORTUNITY: 'Opportunities',
  EVIDENCE: 'Evidence',
  DEADLINE: 'Deadlines',
  FACT: 'Facts you recorded',
  ASSUMPTION: 'Assumptions',
  SCENARIO: 'The decision itself',
  RESOURCE: 'Resources',
  OBLIGATION: 'Obligations',
  DECISION: 'Recorded decisions',
}

function familyLabel(family: string): string {
  return FAMILY_LABELS[family] ?? humanize(family)
}

/**
 * How each applicability verdict is allowed to be spoken about.
 *
 * `COMPARABLE` is the only status under which a family's records mean
 * anything. The other four each say something different about WHY there is
 * nothing to read, and collapsing them into one grey "none" would destroy the
 * distinction this screen exists to preserve — in particular between a concept
 * that does not exist for a single decision and one that exists but has no
 * sealed source yet.
 */
const APPLICABILITY: Record<
  string,
  { compared: boolean; tone: string; label: string; note: string }
> = {
  COMPARABLE: {
    compared: true,
    tone: 'info',
    label: 'Compared',
    note: 'Both sides carry this family and it means the same thing on each, so any difference here is a real one.',
  },
  NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON: {
    compared: false,
    tone: 'excluded',
    label: 'Does not apply',
    note: 'This belongs to a portfolio of several actions taken together. One decision on its own has nothing of this kind to compare, so this section is empty by definition rather than by result.',
  },
  CURRENT_SOURCE_UNAVAILABLE: {
    compared: false,
    tone: 'constrained',
    label: 'No source to compare',
    note: 'This is meaningful for a single decision, but nothing Onyx seals today produces it at this level. It is missing, which is not the same as unchanged.',
  },
  READINESS_SEMANTICS_ONLY: {
    compared: false,
    tone: 'constrained',
    label: 'Readiness only',
    note: 'Sealed records keep whether a document requirement was ready, not which document satisfied it, so identity is not compared here. Readiness itself is reported under Evidence.',
  },
  RESERVED_NO_PRODUCER: {
    compared: false,
    tone: 'excluded',
    label: 'Not in use',
    note: 'This family is reserved in the Onyx model and nothing produces it, so there is nothing on either side to compare.',
  },
}

function applicabilityCopy(status: string | undefined) {
  return (
    (status ? APPLICABILITY[status] : undefined) ?? {
      compared: false,
      tone: 'neutral',
      label: 'Not compared',
      note: 'Onyx did not compare this family for this decision.',
    }
  )
}

/** The comparison's closed change vocabulary, phrased in the same before/after
 *  terms the field table uses. */
const CHANGE_KINDS: Record<string, { tone: string; label: string; note: string }> = {
  ADDED: {
    tone: 'attention',
    label: 'Added',
    note: 'This exists in the modelled result and not in your current position.',
  },
  REMOVED: {
    tone: 'attention',
    label: 'Removed',
    note: 'This exists in your current position and not in the modelled result.',
  },
  CHANGED: {
    tone: 'info',
    label: 'Changed',
    note: 'The fields below are the ones that differ.',
  },
  UNCHANGED: {
    tone: 'neutral',
    label: 'Unchanged',
    note: 'Nothing about this record differs between the two sides.',
  },
}

function changeCopy(change: string) {
  return (
    CHANGE_KINDS[change] ?? {
      tone: 'neutral',
      label: humanize(change),
      note: 'The fields below are the ones the comparison recorded.',
    }
  )
}

/* ------------------------------------------------------- keys and values -- */

/**
 * A record's name, built from its certified key WITHOUT its stored identity.
 *
 * A key is `FAMILY:source_kind:source_id`. The first two segments say what the
 * record IS; the last is a row identifier that means nothing to a customer and
 * must never appear as visible copy. Only the descriptive part is shown, plus
 * any trailing segment that is itself a governed code (a document type, a tax
 * year) rather than an identifier.
 */
const SOURCE_KIND_LABELS: Record<string, string> = {
  'finance.income_source': 'Income',
  'finance.expense_record': 'Expense',
  'wealth.asset': 'Asset',
  'wealth.liability': 'Liability',
  'profile.tax_profile': 'Your tax profile',
  'analysis.analysis_run': 'Estimate for the year',
  'analysis.analysis_line_item': 'Line in the estimate',
  'docs.document': 'Document you hold',
  'rules.rule_required_document': 'Required document',
  'rules.rule_deadline': 'Deadline',
  'ioe.resource_ledger_entry': 'Resource ledger entry',
  'ioe.optimization_candidate': 'Opportunity',
  'ioe.optimization_run.assumption_set': 'Assumption set',
  'ioe.scenario_assumption': 'Assumption',
  'ioe.scenario': 'This decision',
}

const IDENTIFIER = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const LONG_HEX = /^[0-9a-f]{12,}$/i

/** A short governed code (T4, RRSP) is already the customer-facing word; a
 *  longer token is a machine phrase and reads better humanized. */
function segmentLabel(segment: string): string {
  return /^[A-Z0-9]{1,6}$/.test(segment) ? segment : humanize(segment)
}

function describeKey(key: string): string {
  const parts = key.split(':')
  const kind = parts[1] ?? ''
  const base =
    SOURCE_KIND_LABELS[kind] ??
    (kind.length > 0 ? humanize(kind) : humanize(parts[0] ?? key))
  const qualifiers = parts
    .slice(2)
    .filter((part) => part.length > 0 && !IDENTIFIER.test(part) && !LONG_HEX.test(part))
    .map(segmentLabel)
  return qualifiers.length > 0 ? `${base} · ${qualifiers.join(' · ')}` : base
}

/** `attributes.taxable_income` → `Taxable income`. The namespace prefix is
 *  storage mechanics; the field name is what the reader needs. */
function fieldLabel(field: string): string {
  const bare = field.startsWith('attributes.')
    ? field.slice('attributes.'.length)
    : field
  return humanize(bare)
}

/**
 * A comparison value, exactly as it was sealed.
 *
 * Deliberately NOT run through `money()` or `percent()`. The contract does not
 * say which unit a field carries, and a frontend that guessed would sooner or
 * later print a tax year as a dollar amount. The canonical string is the value
 * the seal recorded, so the canonical string is what is shown.
 */
function ComparisonValue({ value }: { value: string | null | undefined }) {
  if (value === null || value === undefined || value === '') {
    return (
      <>
        <span aria-hidden="true">—</span>
        <span className="sr-only">Not present on this side</span>
      </>
    )
  }
  return <>{value}</>
}

/* --------------------------------------------------------- the top frame -- */

function ComparisonFrame({
  comparison,
  scenario,
}: {
  comparison: BeforeYouActComparisonOut
  scenario: ScenarioDetailOut | undefined
}) {
  const baseline = scenario?.baseline_tax ?? null
  const modelled = scenario?.scenario_tax ?? null
  const delta = scenario?.tax_delta ?? null
  const levers = scenario?.levers ?? []
  const context = comparison.scenario

  return (
    <div className="stack stack-5">
      <div className="twin">
        <div className="twin__side">
          <span className="twin__label">Current</span>
          {baseline ? (
            <>
              <span className="figure figure--lg tabular">
                {money(baseline.amount)}
              </span>
              <p className="text-xs text-muted" style={{ marginTop: 'var(--space-2)' }}>
                Estimated tax as things stand
              </p>
            </>
          ) : (
            <p className="text-sm text-secondary">
              Your position as Onyx has it recorded today.
            </p>
          )}
        </div>

        {/* The decision sits between the two sides. It is what you are
            proposing, stated in the levers the registry accepted — never an
            engine field name. */}
        <div className="twin__delta">
          <span className="twin__delta-caption">Decision</span>
          {levers.length === 0 ? (
            <span className="text-sm text-secondary">
              {context.label ?? 'One modelled decision'}
            </span>
          ) : (
            <div className="stack stack-2">
              {levers.map((lever) => (
                <span className="text-sm" key={lever.lever_code}>
                  {humanize(lever.lever_code)}
                  {Object.entries(lever.parameters ?? {}).map(([name, value]) => (
                    <span className="text-xs text-muted" key={name}>
                      {' · '}
                      {humanize(name)}{' '}
                      <span className="tabular">
                        {name === 'amount' ? money(value) : value}
                      </span>
                    </span>
                  ))}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="twin__side twin__side--modelled">
          <span className="twin__label">Result</span>
          {modelled ? (
            <>
              <span className="figure figure--lg tabular">
                {money(modelled.amount)}
              </span>
              <p className="text-xs text-muted" style={{ marginTop: 'var(--space-2)' }}>
                Estimated tax if you did this
              </p>
            </>
          ) : (
            <p className="text-sm text-secondary">
              Everything the comparison found is set out below, record by
              record.
            </p>
          )}
          {delta ? (
            <p className="text-xs text-muted" style={{ marginTop: 'var(--space-2)' }}>
              Difference sealed with the result:{' '}
              <span className="tabular">{money(delta.amount, { signed: true })}</span>{' '}
              ({humanize(delta.effect_type)})
            </p>
          ) : null}
        </div>
      </div>

      <div className="row row-3 wrap">
        <Provenance kind="calculated" />
        {context.tax_year ? (
          <span className="text-xs text-muted">Tax year {context.tax_year}</span>
        ) : null}
        {context.jurisdiction ? (
          <span className="text-xs text-muted">
            Jurisdiction {context.jurisdiction}
          </span>
        ) : null}
        {context.completed_at ? (
          <span className="text-xs text-muted">
            Modelled {isoDate(context.completed_at)}
          </span>
        ) : null}
      </div>

      <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
        {comparison.direction === 'BASELINE_TO_COUNTERFACTUAL'
          ? 'Everything below reads in one direction: from the position you are in now to the one this decision would produce. Both sides were sealed by the Onyx engine, and this screen only reports the difference between them.'
          : `Direction of this comparison: ${humanize(comparison.direction)}.`}
      </p>
    </div>
  )
}

/* ------------------------------------------------------------- the counts -- */

const CHANGE_ORDER = ['ADDED', 'REMOVED', 'CHANGED', 'UNCHANGED']

/** Every kind either map mentions, in the vocabulary's own order. Presentation
 *  ordering only: no count is recalculated, combined or re-ranked. */
function orderedKinds(...maps: Record<string, number>[]): string[] {
  const seen = new Set<string>()
  for (const map of maps) for (const key of Object.keys(map)) seen.add(key)
  const known = CHANGE_ORDER.filter((kind) => seen.has(kind))
  const rest = [...seen].filter((kind) => !CHANGE_ORDER.includes(kind)).sort()
  return [...known, ...rest]
}

function ComparisonCounts({ comparison }: { comparison: BeforeYouActComparisonOut }) {
  const { summary } = comparison
  const kinds = orderedKinds(
    summary.node_counts_by_change,
    summary.edge_counts_by_change,
  )

  return (
    <div className="stack stack-5">
      <div className="table-scroll">
        <table className="data-table">
          <caption className="sr-only">
            Counts across the whole comparison, by change type
          </caption>
          <thead>
            <tr>
              <th scope="col">Change</th>
              <th scope="col" className="numeric">
                Records
              </th>
              <th scope="col" className="numeric">
                Connections between them
              </th>
            </tr>
          </thead>
          <tbody>
            {kinds.length === 0 ? (
              <tr>
                <th scope="row">No change recorded</th>
                <td className="numeric">0</td>
                <td className="numeric">0</td>
              </tr>
            ) : (
              kinds.map((kind) => (
                <tr key={kind}>
                  <th scope="row">{changeCopy(kind).label}</th>
                  <td className="numeric tabular">
                    {plainNumber(summary.node_counts_by_change[kind] ?? 0)}
                  </td>
                  <td className="numeric tabular">
                    {plainNumber(summary.edge_counts_by_change[kind] ?? 0)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="stack stack-2">
        <span className="eyebrow">Families that moved</span>
        {summary.changed_families.length === 0 ? (
          <p className="text-sm text-secondary">
            No family moved. Every record the comparison examined is the same on
            both sides.
          </p>
        ) : (
          <div className="row row-2 wrap">
            {summary.changed_families.map((family) => (
              <span className="status status--info" key={family}>
                {familyLabel(family)}
              </span>
            ))}
          </div>
        )}
      </div>

      <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
        These counts describe the whole comparison. The sections below show{' '}
        {comparison.includes_unchanged
          ? 'every record, including the ones that did not move'
          : 'only the records that moved, so a count here can be larger than the list below'}
        .
      </p>
    </div>
  )
}

/* ------------------------------------------------------------ one record -- */

function FieldTable({ fields, label }: { fields: FieldChange[]; label: string }) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <caption className="sr-only">Fields that differ for {label}</caption>
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Before</th>
            <th scope="col">After</th>
            <th scope="col">Delta</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((field) => {
            /* Supplied or absent. An absent delta is never filled in here: the
               domain defines no difference for that field, and subtracting the
               two sides would invent one. */
            const delta = field.delta ?? null
            return (
              <tr key={field.field}>
                <th scope="row">{fieldLabel(field.field)}</th>
                <td className="tabular">
                  <ComparisonValue value={field.before} />
                </td>
                <td className="tabular">
                  <ComparisonValue value={field.after} />
                </td>
                <td className="tabular">
                  {delta === null ? (
                    <>
                      <span aria-hidden="true">—</span>
                      <span className="sr-only">
                        No difference is defined for this field
                      </span>
                    </>
                  ) : (
                    delta
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ChangeRecordBlock({ record }: { record: ChangeRecord }) {
  const copy = changeCopy(record.change)
  const fields = record.fields ?? []
  const name = describeKey(record.key)

  return (
    <article className="stack stack-3">
      <div className="row row-3 wrap">
        <h3 className="change__subject">{name}</h3>
        <span className={`status status--${copy.tone}`}>{copy.label}</span>
      </div>
      <p className="text-sm text-secondary">{copy.note}</p>
      {fields.length > 0 ? <FieldTable fields={fields} label={name} /> : null}
    </article>
  )
}

/* ------------------------------------------------------------ one family -- */

function FamilySection({
  family,
  applicability,
  records,
}: {
  family: string
  applicability: Applicability | undefined
  records: ChangeRecord[] | null
}) {
  const copy = applicabilityCopy(applicability?.status)
  const label = familyLabel(family)
  const headingId = `family-${family.toLowerCase()}`

  /* A family that does not participate is drawn on sunken ground with its own
     word for why. A comparable family that happens to hold nothing keeps the
     ordinary surface and says so in the ordinary way. The two must not look
     alike — that difference is the point of this screen. */
  if (!copy.compared || records === null) {
    return (
      <section className="panel panel--sunken" aria-labelledby={headingId}>
        <div className="panel__header">
          <h2 className="section-title" id={headingId}>
            {label}
          </h2>
          <span className={`status status--${copy.tone}`}>{copy.label}</span>
        </div>
        <div className="panel__body">
          <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
            {copy.compared
              ? 'Onyx compares this family, but this screen does not receive its records, so there is nothing here to read.'
              : copy.note}
          </p>
          {applicability ? (
            <p className="text-xs text-muted" style={{ marginTop: 'var(--space-3)' }}>
              Recorded reason: {humanize(applicability.reason_code)}
            </p>
          ) : null}
        </div>
      </section>
    )
  }

  return (
    <section className="panel" aria-labelledby={headingId}>
      <div className="panel__header">
        <div>
          <h2 className="section-title" id={headingId}>
            {label}
          </h2>
          <p className="text-sm text-muted" style={{ marginTop: 'var(--space-1)' }}>
            {records.length === 0
              ? 'Compared, nothing differs'
              : `${plainNumber(records.length)} ${
                  records.length === 1 ? 'record' : 'records'
                } listed`}
          </p>
        </div>
        <span className={`status status--${copy.tone}`}>{copy.label}</span>
      </div>

      <div className="panel__body">
        {records.length === 0 ? (
          <EmptyState
            title="Nothing here would move"
            body="Onyx compared this family on both sides of the decision and found nothing that differs. That is a result, not a gap: this part of your position stays as it is."
          />
        ) : (
          <div className="stack stack-5">
            {records.map((record, index) => (
              <div className="stack stack-5" key={record.key}>
                {index > 0 ? <hr className="divider" /> : null}
                <ChangeRecordBlock record={record} />
              </div>
            ))}
          </div>
        )}
      </div>

      {applicability ? (
        <div className="panel__footer">
          <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
            Comparable because: {humanize(applicability.reason_code)}.
          </p>
        </div>
      ) : null}
    </section>
  )
}

/* -------------------------------------------------------- AI explanation -- */

function ComparisonExplanation({ scenarioId }: { scenarioId: string }) {
  const [asked, setAsked] = useState(false)
  const explanation = useExplanation({
    type: 'COMPARISON',
    subjectId: scenarioId,
    enabled: asked,
  })

  return (
    <div className="stack stack-4">
      <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
        Onyx can put this comparison into words: what moved, what it depends on,
        and what it does not tell you. The records above do not change — the
        wording is written from the same ones.
      </p>

      <div>
        <button
          type="button"
          className="btn btn--secondary"
          onClick={() => setAsked((value) => !value)}
          aria-expanded={asked}
          aria-controls="comparison-explanation"
        >
          {asked ? 'Hide explanation' : 'Explain what would change'}
        </button>
      </div>

      <div id="comparison-explanation">
        {!asked ? null : explanation.isPending ? (
          <LoadingBlock label="Writing an explanation of this comparison" />
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

/* ---------------------------------------------------------------- page -- */

/** Section order comes from the backend's own applicability list, which covers
 *  every family in the model — including the ones with no bucket in this
 *  contract, which is exactly how a reader learns they were not compared. Any
 *  bucket the list did not mention is appended rather than silently dropped. */
function familySections(comparison: BeforeYouActComparisonOut) {
  const listed = comparison.family_applicability.map((entry) => ({
    family: entry.family,
    applicability: entry,
  }))
  const mentioned = new Set(listed.map((entry) => entry.family))
  const missing = Object.keys(FAMILY_BUCKETS)
    .filter((family) => !mentioned.has(family))
    .map((family) => ({ family, applicability: undefined }))
  return [...listed, ...missing]
}

export default function BeforeYouAct() {
  const { scenarioId } = useParams<{ scenarioId: string }>()
  const comparison = useComparison(scenarioId)
  const scenario = useScenario(scenarioId)

  return (
    <>
      <PageHead
        eyebrow="Before you act"
        title="What this decision would change"
        lede="The sealed comparison between the position you are in and the one this decision would produce, set out record by record."
        actions={
          scenarioId ? (
            <Link className="btn btn--ghost" to={`/app/twin/${scenarioId}`}>
              Back to the Decision Twin
            </Link>
          ) : null
        }
      />

      <div className="stack stack-6">
        {!scenarioId ? (
          <section className="panel">
            <div className="panel__body">
              <EmptyState
                title="No decision named"
                body="This page describes one modelled decision. Open a model from the Decision Twin to see what it would change."
                action={
                  <Link className="btn btn--primary" to="/app/twin">
                    Go to the Decision Twin
                  </Link>
                }
              />
            </div>
          </section>
        ) : (
          <AsyncBlock query={comparison}>
            {(data) => (
              <div className="stack stack-6">
                {/* ------------------------- current → decision → result */}
                <section className="panel" aria-labelledby="frame-heading">
                  <div className="panel__header">
                    <h2 className="section-title" id="frame-heading">
                      Current, decision, result
                    </h2>
                  </div>
                  <div className="panel__body">
                    {scenario.isPending ? (
                      <LoadingBlock label="Loading the modelled decision" />
                    ) : (
                      <ComparisonFrame comparison={data} scenario={scenario.data} />
                    )}
                  </div>
                  {scenario.data ? (
                    <div className="panel__footer">
                      {/* Two different questions, never merged: is this still
                          true, and can it still be reproduced. */}
                      <TrustPair
                        freshness={scenario.data.freshness?.freshness_status}
                        integrity={scenario.data.integrity?.integrity_state}
                      />
                    </div>
                  ) : null}
                </section>

                {/* ---------------------------------------- what moved */}
                <section className="panel" aria-labelledby="counts-heading">
                  <div className="panel__header">
                    <div>
                      <h2 className="section-title" id="counts-heading">
                        How much moved
                      </h2>
                      <p
                        className="text-sm text-muted"
                        style={{ marginTop: 'var(--space-1)' }}
                      >
                        Counts the engine produced over the full comparison.
                      </p>
                    </div>
                  </div>
                  <div className="panel__body">
                    <ComparisonCounts comparison={data} />
                  </div>
                </section>

                {/* ------------------------------------ family by family */}
                {familySections(data).map((section) => {
                  const bucket = FAMILY_BUCKETS[section.family]
                  return (
                    <FamilySection
                      key={section.family}
                      family={section.family}
                      applicability={section.applicability}
                      records={bucket ? bucket(data) : null}
                    />
                  )
                })}

                {/* ------------------------------------------ AI wording */}
                <section className="panel" aria-labelledby="explain-heading">
                  <div className="panel__header">
                    <h2 className="section-title" id="explain-heading">
                      In words
                    </h2>
                  </div>
                  <div className="panel__body">
                    <ComparisonExplanation scenarioId={scenarioId} />
                  </div>
                </section>

                {/* ------------------------------------------ the caveat */}
                <section className="panel panel--sunken" aria-labelledby="limits-heading">
                  <div className="panel__header">
                    <h2 className="section-title" id="limits-heading">
                      What this screen does not say
                    </h2>
                  </div>
                  <div className="panel__body">
                    <div className="stack stack-3" style={{ maxWidth: '68ch' }}>
                      <p className="text-sm text-secondary">
                        This is a description of a difference, not a
                        recommendation. Onyx does not rank this decision, score
                        it, or say whether you should take it.
                      </p>
                      <p className="text-sm text-secondary">
                        Values are shown exactly as they were sealed, in the
                        scale the engine recorded them. An em dash in the delta
                        column means the domain defines no difference for that
                        field — it does not mean the difference is nothing.
                      </p>
                      <p className="text-xs text-muted">
                        Comparison format {data.schema_version}
                        {data.scenario.result_schema_version
                          ? ` · result format ${data.scenario.result_schema_version}`
                          : ''}
                      </p>
                    </div>
                  </div>
                </section>
              </div>
            )}
          </AsyncBlock>
        )}
      </div>
    </>
  )
}
