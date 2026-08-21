/* =========================================================================
   THE ONYX TAX DECISION TWIN  —  the flagship interaction
   =========================================================================
   Two positions, side by side: the one you are in, and the one a single
   registry-controlled lever would put you in. The delta between them is the
   loudest thing on the screen, because the delta is the decision.

   THE ONE RULE THIS FILE IS BUILT AROUND: the twin never subtracts anything.
   `baseline_tax`, `scenario_tax` and `tax_delta` all arrive from the sealed
   scenario, and the screen's entire contribution is deciding which side of the
   connector each one sits on. A frontend that computed the difference itself
   would be a second tax engine with none of the governance, and it would drift
   from the sealed figure the moment either side changed.

   THE REFUSAL IS PART OF THE PRODUCT. Asking to model more contribution room
   than Onyx can see is answered with a 409, and that answer is rendered as a
   calm explanation with two ways forward — not as a crash, and not as a
   quietly clamped number the customer never asked for.
   ========================================================================= */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { PageHead, useTaxYear } from '@/components/Shell'
import {
  AsyncBlock,
  EmptyState,
  ErrorState,
  LoadingBlock,
  describeError,
} from '@/components/states'
import {
  AiExplanation,
  AssumptionNote,
  FreshnessBadge,
  Provenance,
  SupportSignal,
  TrustPair,
} from '@/components/trust'
import { humanize, isoDate, magnitude, money } from '@/lib/format'
import {
  latestAnalysisFor,
  useAnalyses,
  useCreateScenario,
  useScenario,
  useScenarios,
} from '@/lib/queries'
import { useExplanation } from '@/lib/explanation'
import { ApiError } from '@/api/client'
import type { ScenarioDetailOut, ScenarioSummaryOut } from '@/api/endpoints'

/** The sealed money shape, taken from the contract rather than restated. */
type Monetary = NonNullable<ScenarioDetailOut['tax_delta']>

/* ------------------------------------------------------------ the bench -- */

interface LeverOption {
  code: string
  label: string
  description: string
  amountLabel: string
  /** Contribution levers draw on registered-account room, which is the thing
   *  the backend refuses to exceed. Only those offer the room declaration —
   *  a donation is not room a taxpayer holds. */
  declaresRoom: boolean
}

/**
 * The levers this screen offers.
 *
 * The CODES belong to the backend's lever registry; the labels and one-line
 * descriptions are presentation. The list is hard-coded rather than assembled
 * from anything the customer can influence: an unregistered code is refused,
 * and offering one here would be an invitation to a refusal.
 */
const LEVERS = [
  {
    code: 'INCREASE_RRSP_DEDUCTION',
    label: 'RRSP contribution',
    description: 'Contribute to an RRSP and deduct it in this tax year.',
    amountLabel: 'Amount to contribute',
    declaresRoom: true,
  },
  {
    code: 'INCREASE_FHSA_DEDUCTION',
    label: 'FHSA contribution',
    description:
      'Contribute to a first home savings account and deduct it in this tax year.',
    amountLabel: 'Amount to contribute',
    declaresRoom: true,
  },
  {
    code: 'INCREASE_DONATIONS',
    label: 'Charitable donation',
    description: 'Make an eligible donation to a registered charity.',
    amountLabel: 'Amount to donate',
    declaresRoom: false,
  },
] as const satisfies readonly LeverOption[]

function leverLabel(code: string): string {
  return LEVERS.find((lever) => lever.code === code)?.label ?? humanize(code)
}

/** Engine input fields, given the names a person uses for them. Unknown fields
 *  fall through to the generic humaniser rather than being hidden. */
const FIELD_LABELS: Record<string, string> = {
  rrsp_deduction: 'RRSP deduction',
  fhsa_deduction: 'FHSA deduction',
  donations: 'Donations claimed',
}

function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? humanize(field)
}

const AMOUNT_ID = 'twin-amount'
const ROOM_ID = 'twin-room'

/**
 * A non-negative decimal STRING, matched against the backend's own limits.
 *
 * The regular expression is the whole validation. The value is never put
 * through `Number()` on its way to the API: the string the customer typed is
 * the string the backend receives, because turning "1234.10" into a float and
 * back is how a cent goes missing between a form and a tax return.
 */
const AMOUNT_SHAPE = /^\d{1,12}(\.\d{1,2})?$/

const AMOUNT_GUIDANCE =
  'Enter dollars and cents, such as 4200 or 4200.50. No dollar sign, commas or minus sign.'

function amountProblem(raw: string): string | null {
  const value = raw.trim()
  if (!value) return 'Enter an amount.'
  if (!AMOUNT_SHAPE.test(value)) return AMOUNT_GUIDANCE
  return null
}

interface Problem {
  field: string
  message: string
}

/* ------------------------------------------------------- delta direction -- */

export interface DeltaTone {
  modifier: string
  arrow: string
  word: string
  /** The same movement as a sentence fragment, for the live announcement. */
  spoken: string
}

/** Effect types whose amount is stated as a BENEFIT: a positive figure is a
 *  reduction in what you owe, not an increase. `current_year_tax_reduction` is
 *  computed by the engine as `baseline_tax - scenario_tax`. */
const REDUCTION_EFFECTS = new Set(['current_year_tax_reduction'])

/**
 * Which way the connector points.
 *
 * READ THE EFFECT TYPE, NOT JUST THE SIGN. `tax_delta` is not an ordinary
 * signed difference — the engine computes `baseline_tax - scenario_tax` and
 * labels it `current_year_tax_reduction`, so a POSITIVE amount means the
 * modelled tax is LOWER. Treating it as a plain difference inverted this badge
 * completely: it told a customer that an $8,000 RRSP contribution raised their
 * tax by $2,403.79, in red, with an up arrow, next to a caption correctly
 * reading "current year tax reduction" — and said the same thing to screen
 * readers.
 *
 * A figure whose effect type is not a known benefit gets NO directional claim.
 * Guessing a direction from a sign whose meaning we have not established is
 * precisely what produced the inversion.
 *
 * Nothing here derives a delta — the amount rendered is always
 * `tax_delta.amount`, and the arrow is aria-hidden because the WORD beside it
 * is what carries the direction.
 */
export function deltaTone(delta: Monetary): DeltaTone {
  const numeric = Number(delta.amount)

  if (!REDUCTION_EFFECTS.has(delta.effect_type) || !Number.isFinite(numeric)) {
    const kind = humanize(delta.effect_type)
    return {
      modifier: '',
      arrow: '·',
      word: kind,
      spoken: `${magnitude(delta.amount)}, ${kind.toLowerCase()}`,
    }
  }

  if (numeric === 0) {
    return { modifier: '', arrow: '=', word: 'No change', spoken: 'unchanged' }
  }

  if (numeric > 0) {
    return {
      modifier: ' twin__delta--down',
      arrow: '↓',
      word: 'Lower',
      spoken: `${magnitude(delta.amount)} lower`,
    }
  }

  return {
    modifier: ' twin__delta--up',
    arrow: '↑',
    word: 'Higher',
    spoken: `${magnitude(delta.amount)} higher`,
  }
}

/* --------------------------------------------------------- ready result -- */

interface ReadyScenario {
  scenario: ScenarioDetailOut
  baseline: Monetary
  modelled: Monetary
  delta: Monetary
}

/** A scenario is only shown as a twin when the run FINISHED and all three
 *  sealed figures are present. A half-populated twin would invite the reader
 *  to fill the missing side in themselves. */
function readyScenario(scenario: ScenarioDetailOut | undefined): ReadyScenario | null {
  if (!scenario) return null
  if (scenario.workflow_status !== 'completed') return null
  const { baseline_tax: baseline, scenario_tax: modelled, tax_delta: delta } = scenario
  if (!baseline || !modelled || !delta) return null
  return { scenario, baseline, modelled, delta }
}

/* ================================================================ bench == */

function Bench({
  analysisId,
  taxYear,
  create,
}: {
  analysisId: string
  taxYear: number
  create: ReturnType<typeof useCreateScenario>
}) {
  const navigate = useNavigate()
  const [leverCode, setLeverCode] = useState<string>(LEVERS[0].code)
  const [amount, setAmount] = useState('')
  const [declareRoom, setDeclareRoom] = useState(false)
  const [room, setRoom] = useState('')
  const [problems, setProblems] = useState<readonly Problem[]>([])
  const summaryRef = useRef<HTMLDivElement>(null)

  const selected = LEVERS.find((lever) => lever.code === leverCode) ?? LEVERS[0]

  /* The declaration only counts while the lever it belongs to is the one
     selected. Reading the checkbox on its own would let a control the customer
     can no longer see put an assumption into the request. */
  const declaring = selected.declaresRoom && declareRoom

  // A failed submission moves focus to the summary, so a keyboard or screen
  // reader user meets the problem instead of hunting for it.
  useEffect(() => {
    if (problems.length > 0) summaryRef.current?.focus()
  }, [problems])

  const refusal =
    create.error instanceof ApiError && create.error.isConflict ? create.error : null

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const found: Problem[] = []
    const amountIssue = amountProblem(amount)
    if (amountIssue) found.push({ field: AMOUNT_ID, message: amountIssue })
    if (declaring) {
      const roomIssue = amountProblem(room)
      if (roomIssue) found.push({ field: ROOM_ID, message: roomIssue })
    }
    setProblems(found)
    if (found.length > 0) return

    const trimmedAmount = amount.trim()
    create.mutate(
      {
        analysis_id: analysisId,
        levers: [{ lever_code: selected.code, parameters: { amount: trimmedAmount } }],
        label: `${selected.label} of ${money(trimmedAmount)}`,
        // The declaration is the CUSTOMER'S OWN statement. The backend records
        // it as `user_asserted`, which is why the scenario that comes back
        // says "you stated this" rather than presenting it as verified room.
        ...(declaring
          ? {
              assumptions: [
                {
                  assumption_code: 'CONTRIBUTION_ROOM_AVAILABLE',
                  value_number: room.trim(),
                  materiality: 'high',
                },
              ],
            }
          : {}),
      },
      { onSuccess: (result) => navigate(`/app/twin/${result.id}`) },
    )
  }

  const amountProblemText = problems.find((p) => p.field === AMOUNT_ID)?.message
  const roomProblemText = problems.find((p) => p.field === ROOM_ID)?.message

  return (
    <form className="stack stack-5" noValidate onSubmit={onSubmit}>
      {problems.length > 0 ? (
        <div className="error-summary" ref={summaryRef} role="alert" tabIndex={-1}>
          <h3 className="text-sm" style={{ marginBottom: 'var(--space-2)' }}>
            Check what you entered
          </h3>
          <ul style={{ paddingLeft: 'var(--space-4)' }}>
            {problems.map((problem) => (
              <li className="text-sm" key={problem.field}>
                <a href={`#${problem.field}`}>{problem.message}</a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* ------------------------------------------------------- the lever */}
      <div
        aria-labelledby="twin-lever-legend"
        className="stack stack-3"
        role="radiogroup"
      >
        <span className="eyebrow" id="twin-lever-legend">
          Choose one lever
        </span>
        {LEVERS.map((lever) => {
          const active = lever.code === leverCode
          const id = `twin-lever-${lever.code.toLowerCase()}`
          return (
            <label
              className={`lever${active ? ' lever--active' : ''}`}
              htmlFor={id}
              key={lever.code}
            >
              <span className="lever__head">
                <span className="row row-2">
                  <input
                    checked={active}
                    id={id}
                    name="twin-lever"
                    onChange={() => setLeverCode(lever.code)}
                    type="radio"
                    value={lever.code}
                  />
                  <span className="lever__name">{lever.label}</span>
                </span>
                {active ? <span className="status status--info">Selected</span> : null}
              </span>
              <span className="text-sm text-secondary">{lever.description}</span>
            </label>
          )
        })}
      </div>

      {/* ------------------------------------------------------ the amount */}
      <div className="field">
        <label className="field__label" htmlFor={AMOUNT_ID}>
          {selected.amountLabel}
        </label>
        <input
          aria-describedby={
            amountProblemText ? `${AMOUNT_ID}-hint ${AMOUNT_ID}-error` : `${AMOUNT_ID}-hint`
          }
          aria-invalid={amountProblemText ? true : undefined}
          autoComplete="off"
          className="field__control tabular"
          id={AMOUNT_ID}
          inputMode="decimal"
          onChange={(event) => setAmount(event.target.value)}
          spellCheck={false}
          type="text"
          value={amount}
        />
        <p className="field__hint" id={`${AMOUNT_ID}-hint`}>
          {AMOUNT_GUIDANCE}
        </p>
        {amountProblemText ? (
          <p className="field__error" id={`${AMOUNT_ID}-error`}>
            {amountProblemText}
          </p>
        ) : null}
      </div>

      {/* --------------------------------------- the customer's own room -- */}
      {selected.declaresRoom ? (
        <div className="field">
          {/* Padding rather than a bare checkbox row: the whole line is the
              target, which is what makes it usable on a phone. */}
          <label
            className="row row-2"
            htmlFor="twin-declare-room"
            style={{ padding: 'var(--space-3) 0' }}
          >
            <input
              checked={declareRoom}
              id="twin-declare-room"
              onChange={(event) => setDeclareRoom(event.target.checked)}
              type="checkbox"
            />
            <span className="text-sm">I have more room than the published limit</span>
          </label>

          {declaring ? (
            <div className="stack stack-2">
              <div className="row row-2 wrap">
                <label className="field__label" htmlFor={ROOM_ID}>
                  Contribution room you actually have
                </label>
                <Provenance kind="user" label="Your declaration" />
              </div>
              <input
                aria-describedby={
                  roomProblemText ? `${ROOM_ID}-hint ${ROOM_ID}-error` : `${ROOM_ID}-hint`
                }
                aria-invalid={roomProblemText ? true : undefined}
                autoComplete="off"
                className="field__control tabular"
                id={ROOM_ID}
                inputMode="decimal"
                onChange={(event) => setRoom(event.target.value)}
                spellCheck={false}
                type="text"
                value={room}
              />
              <p className="field__hint" id={`${ROOM_ID}-hint`}>
                Onyx records this as your own statement and does not verify it.
                Your notice of assessment is where the real figure lives.{' '}
                {AMOUNT_GUIDANCE}
              </p>
              {roomProblemText ? (
                <p className="field__error" id={`${ROOM_ID}-error`}>
                  {roomProblemText}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="row row-3 wrap">
        <button className="btn btn--primary" disabled={create.isPending} type="submit">
          {create.isPending ? 'Modelling…' : `Model against your ${taxYear} position`}
        </button>
      </div>

      {/* --------------------------------------------------- the refusal -- */}
      {refusal ? <Refusal error={refusal} lever={selected} /> : null}
      {create.isError && !refusal ? <ErrorState error={create.error} /> : null}
    </form>
  )
}

/**
 * A refusal, rendered as an answer rather than as a failure.
 *
 * The backend returns an enumerated sentence — "contribution lever requests
 * 9000 against available FHSA_ROOM of 8000; declare
 * CONTRIBUTION_ROOM_AVAILABLE if more room exists" — and `describeError`
 * strips the machine prefix off it. It is shown because it is the specific,
 * checkable reason; the paragraphs around it say what it means and what to do
 * next. Nothing was calculated and nothing was saved, and the copy says so.
 */
function Refusal({ error, lever }: { error: ApiError; lever: LeverOption }) {
  const described = describeError(error)
  return (
    <div className="panel panel--sunken" role="alert">
      <div className="panel__body stack stack-3">
        <h3 className="state-block__title">{described.title}</h3>
        <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
          Onyx would not model this. It refuses to seal a figure it cannot stand
          behind, so nothing was calculated and nothing was saved.
        </p>
        <p className="text-sm" style={{ maxWidth: '68ch' }}>
          {described.body}
        </p>
        <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
          {lever.declaresRoom
            ? 'Two ways forward: lower the amount, or tell Onyx how much room you actually have using the declaration above. A declared figure is recorded as your own statement, and the model then says so.'
            : 'Adjust the amount and try again.'}
        </p>
      </div>
    </div>
  )
}

/* ================================================================= twin == */

function Twin({ ready }: { ready: ReadyScenario }) {
  const { scenario, baseline, modelled, delta } = ready
  const tone = deltaTone(delta)

  return (
    <div className="stack stack-5">
      <div className="twin">
        <div className="twin__side">
          <span className="twin__label">Current state</span>
          <span className="figure figure--lg tabular">{money(baseline.amount)}</span>
          <p className="text-xs text-muted" style={{ marginTop: 'var(--space-2)' }}>
            Estimated tax as things stand
          </p>
        </div>

        {/* Keyed on the scenario so a NEW result remounts the connector and the
            settle animation plays exactly once. Under prefers-reduced-motion
            the duration token collapses to 1ms and the figure simply appears. */}
        <div className={`twin__delta${tone.modifier}`} key={`delta-${scenario.id}`}>
          <span aria-hidden="true" className="twin__delta-arrow">
            {tone.arrow}
          </span>
          <span className="twin__delta-value figure--settling">
            <span className="sr-only">Difference: </span>
            {magnitude(delta.amount)}
          </span>
          <span className="twin__delta-caption">{tone.word}</span>
        </div>

        <div className="twin__side twin__side--modelled">
          <span className="twin__label">Modelled state</span>
          <span
            className="figure figure--lg tabular figure--settling"
            key={`modelled-${scenario.id}`}
          >
            {money(modelled.amount)}
          </span>
          <p className="text-xs text-muted" style={{ marginTop: 'var(--space-2)' }}>
            Estimated tax if you did this
          </p>
        </div>
      </div>

      <div className="row row-3 wrap">
        <Provenance kind="calculated" />
        <span className="text-xs text-muted">{humanize(delta.effect_type)}</span>
        <span className="text-xs text-muted">{humanize(delta.calculation_basis)}</span>
      </div>

      <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
        Both figures and the difference between them come from the Onyx tax
        engine, calculated against the same pinned inputs. This screen puts them
        beside each other; it does not work any of them out.
      </p>
    </div>
  )
}

/* =============================================== what the model rests on == */

function ModelBasis({ scenario }: { scenario: ScenarioDetailOut }) {
  const changes = scenario.applied_changes ?? []

  return (
    <div className="stack stack-6">
      {/* ------------------------------------------------------- levers -- */}
      <div className="stack stack-3">
        <span className="eyebrow">Levers applied</span>
        {scenario.levers.length === 0 ? (
          <p className="text-sm text-secondary">
            This model records no levers, so it describes your position
            unchanged.
          </p>
        ) : (
          scenario.levers.map((lever, index) => (
            <div className="lever" key={`${lever.lever_code}-${index}`}>
              <span className="lever__head">
                <span className="lever__name">{leverLabel(lever.lever_code)}</span>
                <Provenance kind="user" label="You chose this" />
              </span>
              <div className="row row-4 wrap">
                {Object.entries(lever.parameters ?? {}).map(([name, value]) => (
                  <span className="text-sm text-secondary" key={name}>
                    {humanize(name)}:{' '}
                    <span className="tabular">
                      {name === 'amount' ? money(value) : value}
                    </span>
                  </span>
                ))}
              </div>
            </div>
          ))
        )}
      </div>

      {/* -------------------------------------------------- assumptions -- */}
      <div className="stack stack-4">
        <span className="eyebrow">Assumptions this model rests on</span>
        {scenario.assumptions.length === 0 ? (
          <p className="text-sm text-secondary">
            This model records no assumptions of its own.
          </p>
        ) : (
          scenario.assumptions.map((assumption) => (
            <AssumptionNote
              assumption={assumption}
              // A null value gets no formatted string at all, so the note
              // falls back to its general wording instead of reading
              // "assumes — is available".
              formattedValue={
                assumption.value_number == null ? null : money(assumption.value_number)
              }
              key={assumption.assumption_code}
            />
          ))
        )}
        <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
          An assumption is not a fact Onyx has checked. Where a figure above is
          marked as assumed, confirm your real number before acting on this
          model.
        </p>
      </div>

      {/* ----------------------------------------------- applied changes -- */}
      {changes.length > 0 ? (
        <div className="stack stack-3">
          <span className="eyebrow">What the levers moved</span>
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">
                Fields the levers changed, with their value before and after
              </caption>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th className="numeric" scope="col">
                    Before
                  </th>
                  <th className="numeric" scope="col">
                    After
                  </th>
                </tr>
              </thead>
              <tbody>
                {changes.map((change) => (
                  <tr key={`${change.apply_order}-${change.field}`}>
                    <th scope="row">{fieldLabel(change.field)}</th>
                    <td className="numeric tabular">{money(change.old_value)}</td>
                    <td className="numeric tabular">{money(change.new_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
            These are the only inputs the levers were allowed to touch. Nothing
            else about your position was altered.
          </p>
        </div>
      ) : null}
    </div>
  )
}

/* ============================================== AI explanation, on demand == */

/**
 * Asked for, never pre-fetched.
 *
 * An explanation is admission-controlled work with a real cost, so the query
 * stays disabled until the customer presses the button. The prose is rendered
 * as plain React children inside `AiExplanation`, which is what keeps model
 * output from ever being mistaken for a calculated field.
 */
function ScenarioExplanation({
  scenarioId,
  taxYear,
}: {
  scenarioId: string
  taxYear: number | null
}) {
  const [asked, setAsked] = useState(false)
  const explanation = useExplanation({
    type: 'SCENARIO',
    subjectId: scenarioId,
    taxYear,
    enabled: asked,
  })

  return (
    <section className="panel" aria-labelledby="twin-explain-heading">
      <div className="panel__header">
        <h2 className="section-title" id="twin-explain-heading">
          Why the figure moved
        </h2>
        {!asked ? (
          <button
            className="btn btn--secondary btn--sm"
            onClick={() => setAsked(true)}
            type="button"
          >
            Explain this model
          </button>
        ) : null}
      </div>
      <div className="panel__body">
        {!asked ? (
          <p className="text-sm text-secondary" style={{ maxWidth: '68ch' }}>
            Onyx can put this model into plain language, written from the
            figures it has already calculated. It is produced when you ask for
            it rather than in advance, and it does not decide any amount.
          </p>
        ) : explanation.isPending ? (
          <LoadingBlock label="Writing the explanation" />
        ) : explanation.isError ? (
          <ErrorState error={explanation.error} onRetry={explanation.refetch} />
        ) : explanation.data ? (
          <AiExplanation explanation={explanation.data.explanation} />
        ) : null}
      </div>
    </section>
  )
}

/* ======================================================= earlier models == */

function PriorScenarios({
  scenarios,
  taxYear,
  openId,
}: {
  scenarios: readonly ScenarioSummaryOut[]
  taxYear: number
  openId: string | undefined
}) {
  // Presentation only: this year's active models, newest first. The backend
  // decides what exists; this decides the reading order.
  const visible = scenarios
    .filter((s) => s.tax_year === taxYear && s.visibility_status !== 'archived')
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())

  if (visible.length === 0) {
    return (
      <EmptyState
        title="No earlier models"
        body={`Nothing has been modelled for ${taxYear} yet. Anything you model is kept, so you can come back to it.`}
      />
    )
  }

  return (
    <div className="attention">
      {visible.map((summary, index) => {
        const open = summary.id === openId
        return (
          <Link
            aria-current={open ? 'page' : undefined}
            className="attention__item"
            key={summary.id}
            to={`/app/twin/${summary.id}`}
          >
            <span className="attention__rank">{index + 1}</span>
            <span>
              <span className="attention__title">
                {summary.label ?? `Model from ${isoDate(summary.created_at)}`}
              </span>
              <span className="attention__sub">
                {isoDate(summary.created_at)}
                {open ? ' · open now' : ''}
              </span>
            </span>
            <span className="row row-2 wrap">
              {summary.workflow_status !== 'completed' ? (
                <span className="status status--neutral">
                  {humanize(summary.workflow_status)}
                </span>
              ) : null}
              <FreshnessBadge status={summary.freshness.freshness_status} />
            </span>
          </Link>
        )
      })}
    </div>
  )
}

/* ==================================================================== page */

export default function DecisionTwin() {
  const { taxYear } = useTaxYear()
  const { scenarioId } = useParams<{ scenarioId: string }>()
  const analyses = useAnalyses()
  const scenarios = useScenarios()
  const scenario = useScenario(scenarioId)
  const create = useCreateScenario()

  const analysis = latestAnalysisFor(analyses.data, taxYear)
  const ready = readyScenario(scenario.data)

  // One short sentence for assistive technology when a result lands. The
  // visible twin is not itself a live region: announcing every figure on the
  // screen would bury the one thing that changed.
  const announcement = create.isPending
    ? 'Modelling this decision.'
    : ready
      ? `Result ready. Estimated tax under this model is ${deltaTone(ready.delta).spoken}.`
      : ''

  return (
    <>
      <PageHead
        eyebrow={`Tax year ${taxYear}`}
        title="Decision Twin"
        lede="Set one decision against the position you are already in, and see exactly what the Onyx engine says it would change."
      />

      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>

      <div className="stack stack-6">
        {/* ----------------------------------------------------- the bench */}
        <section className="panel" aria-labelledby="twin-bench-heading">
          <div className="panel__header">
            <h2 className="section-title" id="twin-bench-heading">
              Model a decision
            </h2>
          </div>
          <div className="panel__body">
            {analyses.isPending ? (
              <LoadingBlock label="Loading your position" />
            ) : analyses.isError ? (
              <ErrorState error={analyses.error} onRetry={analyses.refetch} />
            ) : !analysis ? (
              <EmptyState
                title={`No analysis yet for ${taxYear}`}
                body="A model is measured against your current position, so Onyx needs that position first. Add your income and other facts and run an analysis."
                action={
                  <Link className="btn btn--primary" to="/app/onboarding">
                    Add your details
                  </Link>
                }
              />
            ) : (
              <Bench analysisId={analysis.id} create={create} taxYear={taxYear} />
            )}
          </div>
          <div className="panel__footer">
            <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
              If a lever asks for more than the contribution room Onyx can see,
              it refuses to model it rather than showing you a figure you could
              not act on. That refusal explains itself and tells you what to do
              next.
            </p>
          </div>
        </section>

        {/* ---------------------------------------------------- the result */}
        <section className="panel" aria-labelledby="twin-result-heading">
          <div className="panel__header">
            <div>
              <h2 className="section-title" id="twin-result-heading">
                Current state against modelled state
              </h2>
              {ready?.scenario.label ? (
                <p className="text-sm text-muted" style={{ marginTop: 'var(--space-1)' }}>
                  {ready.scenario.label} · modelled {isoDate(ready.scenario.created_at)}
                </p>
              ) : null}
            </div>
          </div>

          <div className="panel__body">
            {create.isPending ? (
              <LoadingBlock label="Running this model" />
            ) : !scenarioId ? (
              <EmptyState
                title="Nothing modelled yet"
                body="Choose a lever above and give it an amount. Onyx will run it against your current position and show both sides."
              />
            ) : scenario.isPending ? (
              <LoadingBlock label="Loading this model" />
            ) : scenario.isError ? (
              <ErrorState error={scenario.error} onRetry={scenario.refetch} />
            ) : ready ? (
              <Twin ready={ready} />
            ) : scenario.data ? (
              <EmptyState
                title={
                  scenario.data.workflow_status === 'completed'
                    ? 'This model has no sealed figures'
                    : 'This model did not produce a result'
                }
                body={
                  scenario.data.error_code
                    ? `Onyx stopped before sealing a figure: ${humanize(scenario.data.error_code)}. Nothing was saved as a result. Model it again to get a fresh answer.`
                    : `This model is recorded as ${humanize(scenario.data.workflow_status).toLowerCase()}, so there is no pair of figures to compare. Model it again to get a fresh answer.`
                }
              />
            ) : null}
          </div>

          {ready ? (
            <div className="panel__footer">
              <div className="stack stack-4">
                {/* Two different questions, never merged: is this still true,
                    and can it still be reproduced. */}
                <TrustPair
                  freshness={ready.scenario.freshness?.freshness_status}
                  integrity={ready.scenario.integrity?.integrity_state}
                />
                <div>
                  <Link
                    className="btn btn--primary"
                    to={`/app/before-you-act/${ready.scenario.id}`}
                  >
                    See exactly what changes
                  </Link>
                </div>
              </div>
            </div>
          ) : null}
        </section>

        {/* ------------------------------------------------ what it rests on */}
        {scenario.data ? (
          <section className="panel" aria-labelledby="twin-basis-heading">
            <div className="panel__header">
              <div>
                <h2 className="section-title" id="twin-basis-heading">
                  What this model rests on
                </h2>
                <p className="text-sm text-muted" style={{ marginTop: 'var(--space-1)' }}>
                  Everything below was sealed with the result and is shown exactly
                  as Onyx recorded it.
                </p>
              </div>
            </div>
            <div className="panel__body">
              <ModelBasis scenario={scenario.data} />
            </div>
            <div className="panel__footer">
              <div className="stack stack-4">
                <SupportSignal support={scenario.data.support} />
                <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
                  {scenario.data.disclaimer}
                </p>
              </div>
            </div>
          </section>
        ) : null}

        {/* ------------------------------------------------- the explanation */}
        {ready ? (
          <ScenarioExplanation
            scenarioId={ready.scenario.id}
            taxYear={ready.scenario.tax_year}
          />
        ) : null}

        {/* ---------------------------------------------------- prior models */}
        <section className="panel" aria-labelledby="twin-prior-heading">
          <div className="panel__header">
            <h2 className="section-title" id="twin-prior-heading">
              Earlier models
            </h2>
          </div>
          <div className="panel__body">
            <AsyncBlock query={scenarios}>
              {(data) => (
                <PriorScenarios
                  openId={scenarioId}
                  scenarios={data}
                  taxYear={taxYear}
                />
              )}
            </AsyncBlock>
          </div>
        </section>
      </div>
    </>
  )
}
