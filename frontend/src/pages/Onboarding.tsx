/* =========================================================================
   ONBOARDING  —  five steps, each one explained where it is asked
   =========================================================================
   The whole screen is a fact-collection surface, and that shapes every
   decision in it:

     · Nothing here is calculated. Each step posts what the customer typed to
       the certified backend and then reads the backend's own record back.
       Amounts travel as STRINGS from the input to the request body — they are
       never parsed into a number, summed, or totalled anywhere on this page.
     · Provinces are limited to the four the engine actually has bracket data
       for. Listing thirteen and failing on nine would be a worse answer than
       saying plainly which four are supported.
     · Every step that asks for something personal carries a "Why we need
       this" disclosure IN PLACE, rather than a privacy policy link. A person
       deciding whether to type their income deserves the reason at the field,
       not three clicks away.
     · Optional steps say they are optional and can be skipped without
       pretending that skipping is a mistake.

   The steps are progressive but not a wizard prison: Back works from every
   step after the first, and going back does not discard what was typed.
   ========================================================================= */
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type RefObject,
} from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { financialsApi, profileApi, type TaxProfileIn } from '@/api/endpoints'
import { PageHead, useTaxYear } from '@/components/Shell'
import { AsyncBlock, EmptyState, describeError } from '@/components/states'
import { AssumptionNote, Provenance } from '@/components/trust'
import { money } from '@/lib/format'
import {
  keys,
  useIncome,
  useProfile,
  useRegisteredAccounts,
  useRunAnalysis,
} from '@/lib/queries'

/* ------------------------------------------------------- closed vocabularies -- */
/* Every list below mirrors a seeded reference table in the backend. A code the
   backend does not know is refused at the API, so offering one here would only
   move the failure later — after the customer had typed an amount. */

interface Option {
  value: string
  label: string
}

/** The provinces Onyx will answer for.
 *
 * NOT simply "the provinces with a bracket table". The engine also carries
 * Quebec brackets, and offering them would have been wrong: Quebec residents
 * pay QPP rather than CPP and pay QPIP premiums, and the engine implements
 * neither — a Quebec customer would have been shown a confident figure computed
 * with the wrong payroll contributions entirely.
 *
 * Of the three below, only Ontario is backed by GOVERNED PUBLISHED brackets;
 * Alberta and British Columbia are computed from the engine's own resident
 * constants. That difference is recorded for the launch-scope decision rather
 * than hidden behind a longer list.
 */
const PROVINCES: readonly Option[] = [
  { value: 'AB', label: 'Alberta' },
  { value: 'BC', label: 'British Columbia' },
  { value: 'ON', label: 'Ontario' },
]

const MARITAL_STATUSES: readonly Option[] = [
  { value: 'single', label: 'Single' },
  { value: 'married', label: 'Married' },
  { value: 'common_law', label: 'Common-law' },
  { value: 'separated', label: 'Separated' },
  { value: 'divorced', label: 'Divorced' },
  { value: 'widowed', label: 'Widowed' },
]

const INCOME_TYPES: readonly Option[] = [
  { value: 'employment', label: 'Employment income' },
  { value: 'self_employment', label: 'Self-employment income' },
  { value: 'rental', label: 'Rental income' },
  { value: 'interest', label: 'Interest income' },
  { value: 'eligible_dividends', label: 'Eligible dividends' },
  { value: 'non_eligible_dividends', label: 'Non-eligible dividends' },
  { value: 'capital_gains', label: 'Capital gains' },
  { value: 'pension', label: 'Pension income' },
]

const REGISTERED_TYPES: readonly { value: 'RRSP' | 'FHSA'; label: string }[] = [
  { value: 'RRSP', label: 'RRSP' },
  { value: 'FHSA', label: 'FHSA' },
]

const EXPENSE_CATEGORIES: readonly Option[] = [
  { value: 'donation', label: 'Charitable donations' },
  { value: 'medical', label: 'Medical expenses' },
  { value: 'tuition', label: 'Tuition' },
  { value: 'childcare', label: 'Child care expenses' },
]

type SituationFlag =
  | 'is_self_employed'
  | 'is_student'
  | 'has_rental_income'
  | 'has_investments'
  | 'owns_home'
  | 'first_time_home_buyer'
  | 'has_disability'

const SITUATION_FLAGS: readonly {
  key: SituationFlag
  id: string
  label: string
  hint?: string
}[] = [
  {
    key: 'is_self_employed',
    id: 'is-self-employed',
    label: 'I earn self-employment or business income',
  },
  { key: 'is_student', id: 'is-student', label: 'I am a student' },
  { key: 'has_rental_income', id: 'has-rental-income', label: 'I receive rental income' },
  {
    key: 'has_investments',
    id: 'has-investments',
    label: 'I hold investments outside a registered account',
  },
  { key: 'owns_home', id: 'owns-home', label: 'I own my home' },
  {
    key: 'first_time_home_buyer',
    id: 'first-time-home-buyer',
    label: 'I consider myself a first-time home buyer',
    hint: 'Onyx records this as you state it. The governed rules decide what, if anything, it opens up.',
  },
  {
    key: 'has_disability',
    id: 'has-disability',
    label: 'I have a disability',
    hint: 'Only tick this if you want Onyx to consider disability-related amounts. Leaving it blank is fine.',
  },
]

const STEPS = [
  { id: 'situation', title: 'Province and situation' },
  { id: 'income', title: 'Income' },
  { id: 'registered', title: 'Registered accounts' },
  { id: 'claims', title: 'Donations and medical' },
  { id: 'review', title: 'Review and run' },
] as const

/* ------------------------------------------------------------- validation -- */

/**
 * A non-negative decimal STRING, matched against the backend's own limits
 * (fourteen digits, two decimal places).
 *
 * The regular expression is the whole validation. The value is never passed
 * through `Number()`, because the string the customer typed is the string the
 * backend must receive: turning "1234.10" into a float and back is how a cent
 * goes missing between a form and a tax return.
 */
const AMOUNT_SHAPE = /^\d{1,12}(\.\d{1,2})?$/

const AMOUNT_GUIDANCE =
  'Enter dollars and cents, such as 4200 or 4200.50. No dollar sign, commas or minus sign.'

function amountProblem(raw: string, required: boolean): string | null {
  const value = raw.trim()
  if (!value) return required ? 'Enter an amount.' : null
  if (!AMOUNT_SHAPE.test(value)) return AMOUNT_GUIDANCE
  return null
}

interface Problem {
  field: string
  message: string
}

function problemFor(problems: readonly Problem[], field: string): string | undefined {
  return problems.find((problem) => problem.field === field)?.message
}

function labelFor(options: readonly Option[], value: string | null | undefined): string {
  if (!value) return 'Not stated'
  return options.find((option) => option.value === value)?.label ?? value
}

/* --------------------------------------------------------- small pieces -- */

/**
 * Privacy explained at the point of collection.
 *
 * The body stays in the DOM and is hidden with the `hidden` attribute rather
 * than unmounted, so `aria-controls` always points at something real and the
 * button's expanded state describes an element that exists.
 */
function WhyDisclosure({ id, children }: { id: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="why">
      <button
        aria-controls={`${id}-why-body`}
        aria-expanded={open}
        className="why__toggle"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        {open ? 'Hide why we need this' : 'Why we need this'}
      </button>
      <div className="why__body" hidden={!open} id={`${id}-why-body`}>
        {children}
      </div>
    </div>
  )
}

function StepErrorSummary({
  error,
  problems,
  summaryRef,
}: {
  error: unknown
  problems: readonly Problem[]
  summaryRef: RefObject<HTMLDivElement | null>
}) {
  const described = error ? describeError(error) : null
  if (problems.length === 0 && !described) return null
  return (
    <div className="error-summary" ref={summaryRef} role="alert" tabIndex={-1}>
      <h3 className="text-sm" style={{ marginBottom: 'var(--space-2)' }}>
        {described ? described.title : 'Check what you entered'}
      </h3>
      {described ? <p className="text-sm text-secondary">{described.body}</p> : null}
      {problems.length > 0 ? (
        <ul style={{ paddingLeft: 'var(--space-4)' }}>
          {problems.map((problem) => (
            <li className="text-sm" key={problem.field}>
              <a href={`#${problem.field}`}>{problem.message}</a>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function SelectField({
  id,
  label,
  options,
  placeholder,
  value,
  onChange,
  problem,
  hint,
}: {
  id: string
  label: string
  options: readonly Option[]
  placeholder: string
  value: string
  onChange: (value: string) => void
  problem?: string | undefined
  hint?: string
}) {
  const hintId = hint ? `${id}-hint` : null
  const errorId = problem ? `${id}-error` : null
  const describedBy = [hintId, errorId].filter(Boolean).join(' ')
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <select
        aria-describedby={describedBy || undefined}
        aria-invalid={problem ? true : undefined}
        className="field__control"
        id={id}
        onChange={(event) => onChange(event.target.value)}
        value={value}
      >
        <option value="">{placeholder}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint && hintId ? (
        <p className="field__hint" id={hintId}>
          {hint}
        </p>
      ) : null}
      {problem && errorId ? (
        <p className="field__error" id={errorId}>
          {problem}
        </p>
      ) : null}
    </div>
  )
}

function AmountField({
  id,
  label,
  value,
  onChange,
  problem,
  hint,
  optional,
}: {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  problem?: string | undefined
  hint?: string
  optional?: boolean
}) {
  const hintId = `${id}-hint`
  const errorId = problem ? `${id}-error` : null
  const describedBy = [hintId, errorId].filter(Boolean).join(' ')
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
        {optional ? <span className="field__optional"> Optional</span> : null}
      </label>
      <input
        aria-describedby={describedBy}
        aria-invalid={problem ? true : undefined}
        autoComplete="off"
        className="field__control tabular"
        id={id}
        inputMode="decimal"
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        type="text"
        value={value}
      />
      <p className="field__hint" id={hintId}>
        {hint ?? AMOUNT_GUIDANCE}
      </p>
      {problem && errorId ? (
        <p className="field__error" id={errorId}>
          {problem}
        </p>
      ) : null}
    </div>
  )
}

function TextField({
  id,
  label,
  value,
  onChange,
  hint,
  optional,
}: {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  hint: string
  optional?: boolean
}) {
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
        {optional ? <span className="field__optional"> Optional</span> : null}
      </label>
      <input
        aria-describedby={`${id}-hint`}
        autoComplete="off"
        className="field__control"
        id={id}
        onChange={(event) => onChange(event.target.value)}
        type="text"
        value={value}
      />
      <p className="field__hint" id={`${id}-hint`}>
        {hint}
      </p>
    </div>
  )
}

/** A checkbox whose whole row is the target: 24px box plus 12px of padding
 *  above and below clears the 44px touch target on a phone. */
function CheckboxRow({
  id,
  label,
  hint,
  checked,
  onChange,
}: {
  id: string
  label: string
  hint?: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  return (
    <label
      className="row row-3"
      htmlFor={id}
      style={{ padding: 'var(--space-3) 0', alignItems: 'flex-start' }}
    >
      <input
        aria-describedby={hint ? `${id}-hint` : undefined}
        checked={checked}
        id={id}
        onChange={(event) => onChange(event.target.checked)}
        style={{
          width: 'var(--space-5)',
          height: 'var(--space-5)',
          flexShrink: 0,
          marginTop: 'var(--space-1)',
        }}
        type="checkbox"
      />
      <span className="stack stack-2">
        <span className="field__label">{label}</span>
        {hint ? (
          <span className="field__hint" id={`${id}-hint`}>
            {hint}
          </span>
        ) : null}
      </span>
    </label>
  )
}

/** The same closing note on every collecting step: these are the customer's
 *  own statements, and Onyx does not pretend to have checked them. */
function StatedByYouNote() {
  return (
    <div className="row row-3 wrap">
      <Provenance kind="user" />
      <span className="text-xs text-muted" style={{ maxWidth: '60ch' }}>
        Onyx records this exactly as you state it. Nothing here is checked
        against the CRA, and you can change it later.
      </span>
    </div>
  )
}

/* ================================================================= page == */

export default function Onboarding() {
  const { taxYear } = useTaxYear()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [step, setStep] = useState(0)
  const [problems, setProblems] = useState<Problem[]>([])
  const [attempt, setAttempt] = useState(0)

  const headingRef = useRef<HTMLHeadingElement>(null)
  const summaryRef = useRef<HTMLDivElement>(null)
  const isFirstRender = useRef(true)

  /* Focus follows the customer to the new step's heading, so a screen-reader
     user is told where they now are instead of being left at the bottom of the
     step they just finished. The first render is skipped — arriving on a page
     that immediately grabs focus is disorienting. */
  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false
      return
    }
    headingRef.current?.focus()
  }, [step])

  /* A counter rather than the error object, so a SECOND rejected attempt with
     the same message still re-announces itself. */
  useEffect(() => {
    if (attempt > 0) summaryRef.current?.focus()
  }, [attempt])

  /* ----------------------------------------------------------- step state -- */

  const [situation, setSituation] = useState<{
    provinceCode: string
    maritalStatus: string
    flags: Record<SituationFlag, boolean>
  }>({
    provinceCode: '',
    maritalStatus: '',
    flags: {
      is_self_employed: false,
      is_student: false,
      has_rental_income: false,
      has_investments: false,
      owns_home: false,
      first_time_home_buyer: false,
      has_disability: false,
    },
  })

  const [incomeForm, setIncomeForm] = useState({ type: '', amount: '', source: '' })
  const [registeredForm, setRegisteredForm] = useState({
    type: '',
    contributions: '',
    room: '',
  })
  const [claimForm, setClaimForm] = useState({ category: '', amount: '', description: '' })

  /* What this session posted to /financials/expenses. There is no list
     endpoint for expenses, so the confirmation below is scoped honestly to
     this visit rather than claiming to be everything on the account. */
  const [claimsAdded, setClaimsAdded] = useState<
    { id: string; categoryCode: string; amount: string; description: string }[]
  >([])

  /* ------------------------------------------------------------- queries -- */

  const profile = useProfile()
  const income = useIncome(taxYear)
  const registered = useRegisteredAccounts(taxYear)

  /* Prefill once, from the record the backend already holds, so a customer who
     comes back does not retype what Onyx knows. Guarded by a ref rather than a
     dependency check: a later refetch must never overwrite what the person is
     part-way through typing. */
  const prefilled = useRef(false)
  useEffect(() => {
    const record = profile.data
    if (prefilled.current || !record) return
    prefilled.current = true
    setSituation((previous) => ({
      provinceCode: record.province_code ?? previous.provinceCode,
      maritalStatus: record.marital_status ?? previous.maritalStatus,
      flags: {
        ...previous.flags,
        is_self_employed: record.is_self_employed,
        is_student: record.is_student,
        has_rental_income: record.has_rental_income,
      },
    }))
  }, [profile.data])

  /* ----------------------------------------------------------- mutations -- */

  const saveProfile = useMutation({
    mutationFn: profileApi.update,
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.profile })
      setProblems([])
      setStep(1)
    },
  })

  const addIncome = useMutation({
    mutationFn: financialsApi.addIncome,
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.income(taxYear) })
      setIncomeForm({ type: '', amount: '', source: '' })
    },
  })

  const addAccount = useMutation({
    mutationFn: financialsApi.addRegisteredAccount,
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.registered(taxYear) })
      setRegisteredForm({ type: '', contributions: '', room: '' })
    },
  })

  const addClaim = useMutation({
    mutationFn: financialsApi.addExpense,
    retry: false,
    onSuccess: (created, variables) => {
      setClaimsAdded((previous) => [
        ...previous,
        {
          id: created.id,
          categoryCode: variables.expense_category_code,
          amount: created.amount,
          description: variables.description ?? '',
        },
      ])
      setClaimForm({ category: '', amount: '', description: '' })
    },
  })

  const runAnalysis = useRunAnalysis()

  /* ---------------------------------------------------------- navigation -- */

  function goToStep(next: number) {
    setProblems([])
    saveProfile.reset()
    addIncome.reset()
    addAccount.reset()
    addClaim.reset()
    runAnalysis.reset()
    setStep(next)
  }

  function reject(found: Problem[]) {
    setProblems(found)
    setAttempt((count) => count + 1)
  }

  /** Written as an explicit assignment rather than a computed spread key so the
   *  flag record keeps its exact key union instead of widening to a string
   *  index signature. */
  function setFlag(key: SituationFlag, checked: boolean) {
    setSituation((previous) => {
      const flags: Record<SituationFlag, boolean> = { ...previous.flags }
      flags[key] = checked
      return { ...previous, flags }
    })
  }

  /* -------------------------------------------------------------- submits -- */

  function submitSituation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found: Problem[] = []
    if (!situation.provinceCode) {
      found.push({ field: 'province', message: 'Choose the province you file in.' })
    }
    if (!situation.maritalStatus) {
      found.push({ field: 'marital-status', message: 'Choose your marital status.' })
    }
    if (found.length > 0) {
      reject(found)
      return
    }
    setProblems([])
    const body: TaxProfileIn = {
      province_code: situation.provinceCode,
      marital_status: situation.maritalStatus,
      ...situation.flags,
    }
    saveProfile.mutate(body, { onError: () => setAttempt((count) => count + 1) })
  }

  function submitIncome(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found: Problem[] = []
    if (!incomeForm.type) {
      found.push({ field: 'income-type', message: 'Choose the kind of income this is.' })
    }
    const amountIssue = amountProblem(incomeForm.amount, true)
    if (amountIssue) found.push({ field: 'income-amount', message: amountIssue })
    if (found.length > 0) {
      reject(found)
      return
    }
    setProblems([])
    const source = incomeForm.source.trim()
    addIncome.mutate(
      {
        tax_year: taxYear,
        income_type_code: incomeForm.type,
        // The trimmed string goes straight into the body. No Number(), no
        // rounding, no re-formatting of the customer's own figure.
        amount: incomeForm.amount.trim(),
        source_name: source === '' ? null : source,
      },
      { onError: () => setAttempt((count) => count + 1) },
    )
  }

  function submitRegistered(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found: Problem[] = []
    /* Read into a local so the narrowing survives to the request body: the
       endpoint accepts a two-member union, not any string. */
    const accountType = registeredForm.type
    const isSupportedType = accountType === 'RRSP' || accountType === 'FHSA'
    if (!isSupportedType) {
      found.push({ field: 'registered-type', message: 'Choose RRSP or FHSA.' })
    }
    const contributionIssue = amountProblem(registeredForm.contributions, true)
    if (contributionIssue) {
      found.push({ field: 'registered-contributions', message: contributionIssue })
    }
    const roomIssue = amountProblem(registeredForm.room, false)
    if (roomIssue) found.push({ field: 'registered-room', message: roomIssue })
    if (found.length > 0 || !(accountType === 'RRSP' || accountType === 'FHSA')) {
      reject(found)
      return
    }
    setProblems([])
    const room = registeredForm.room.trim()
    addAccount.mutate(
      {
        tax_year: taxYear,
        registered_type: accountType,
        contributions_ytd: registeredForm.contributions.trim(),
        contribution_room: room === '' ? null : room,
      },
      { onError: () => setAttempt((count) => count + 1) },
    )
  }

  function submitClaim(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found: Problem[] = []
    if (!claimForm.category) {
      found.push({ field: 'expense-category', message: 'Choose what this amount is for.' })
    }
    const amountIssue = amountProblem(claimForm.amount, true)
    if (amountIssue) found.push({ field: 'expense-amount', message: amountIssue })
    if (found.length > 0) {
      reject(found)
      return
    }
    setProblems([])
    const description = claimForm.description.trim()
    addClaim.mutate(
      {
        tax_year: taxYear,
        expense_category_code: claimForm.category,
        amount: claimForm.amount.trim(),
        description: description === '' ? null : description,
      },
      { onError: () => setAttempt((count) => count + 1) },
    )
  }

  function submitRun() {
    setProblems([])
    runAnalysis.mutate(taxYear, {
      onSuccess: () => navigate('/app', { replace: true }),
      onError: () => setAttempt((count) => count + 1),
    })
  }

  /* ------------------------------------------------------------- rendering -- */

  const current = STEPS[step] ?? STEPS[0]
  const headingId = `${current.id}-heading`

  const activeError =
    step === 0
      ? saveProfile.error
      : step === 1
        ? addIncome.error
        : step === 2
          ? addAccount.error
          : step === 3
            ? addClaim.error
            : runAnalysis.error

  /* An optional step that has already been used says "Continue", not "Skip":
     telling someone they are skipping work they just did reads as if it was
     not recorded. */
  const advanceLabel =
    step === 1
      ? 'Continue'
      : step === 2
        ? (registered.data?.length ?? 0) > 0
          ? 'Continue'
          : 'Skip this step'
        : claimsAdded.length > 0
          ? 'Continue'
          : 'Skip this step'

  return (
    <>
      <PageHead
        eyebrow={`Tax year ${taxYear}`}
        title="Tell Onyx about your year"
        lede="Five short steps. Everything you enter stays in your account, and every step explains why it is asked for."
      />

      <ol aria-label="Onboarding steps" className="steps">
        {STEPS.map((entry, index) => {
          const done = index < step
          const isCurrent = index === step
          const stateWord = done
            ? 'completed'
            : isCurrent
              ? 'current step'
              : 'not started'
          return (
            <li
              aria-current={isCurrent ? 'step' : undefined}
              className={`step${done ? ' step--done' : ''}${isCurrent ? ' step--current' : ''}`}
              key={entry.id}
            >
              <span aria-hidden="true" className="step__dot">
                {index + 1}
              </span>
              <span>{entry.title}</span>
              <span className="sr-only">({stateWord})</span>
            </li>
          )
        })}
      </ol>

      <section aria-labelledby={headingId} className="panel">
        <div className="panel__header">
          <div>
            <span className="eyebrow" id="step-counter">
              Step {step + 1} of {STEPS.length}
            </span>
            {/* Focus lands here on every step change, and the counter is
                described alongside it so the announcement is "Step 2 of 5"
                as well as the step's name. */}
            <h2
              aria-describedby="step-counter"
              className="section-title"
              id={headingId}
              ref={headingRef}
              tabIndex={-1}
            >
              {current.title}
            </h2>
          </div>
        </div>

        <div className="panel__body">
          <div className="stack stack-5">
            <StepErrorSummary
              error={activeError}
              problems={problems}
              summaryRef={summaryRef}
            />

            {/* ============================================ 1. situation == */}
            {step === 0 ? (
              <form className="stack stack-5" noValidate onSubmit={submitSituation}>
                <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
                  Where you file and who you file as decide which brackets,
                  credits and provincial rules apply to everything that follows.
                </p>

                <SelectField
                  hint="Onyx supports Alberta, British Columbia and Ontario today. Quebec files a separate provincial return with its own pension and parental-insurance contributions, which Onyx does not calculate yet, so it is not offered rather than accepted and answered with a wrong figure."
                  id="province"
                  label="Province you file in"
                  onChange={(value) =>
                    setSituation((previous) => ({ ...previous, provinceCode: value }))
                  }
                  options={PROVINCES}
                  placeholder="Select a province"
                  problem={problemFor(problems, 'province')}
                  value={situation.provinceCode}
                />

                <SelectField
                  id="marital-status"
                  label="Marital status"
                  onChange={(value) =>
                    setSituation((previous) => ({ ...previous, maritalStatus: value }))
                  }
                  options={MARITAL_STATUSES}
                  placeholder="Select your marital status"
                  problem={problemFor(problems, 'marital-status')}
                  value={situation.maritalStatus}
                />

                <div
                  aria-labelledby="situation-flags-label"
                  className="stack"
                  role="group"
                >
                  <span className="field__label" id="situation-flags-label">
                    Which of these describe you?
                    <span className="field__optional"> Tick any that apply</span>
                  </span>
                  {SITUATION_FLAGS.map((flag) => (
                    <CheckboxRow
                      checked={situation.flags[flag.key]}
                      hint={flag.hint}
                      id={flag.id}
                      key={flag.key}
                      label={flag.label}
                      onChange={(checked) => setFlag(flag.key, checked)}
                    />
                  ))}
                </div>

                <WhyDisclosure id="situation">
                  <p>
                    Province and marital status are the two facts the tax engine
                    cannot work without: they select the bracket table and the
                    credits Onyx is allowed to consider. The situation ticks,
                    including the disability one, only widen or narrow which
                    rules are examined.
                  </p>
                  <p style={{ marginTop: 'var(--space-2)' }}>
                    All of it stays in your Onyx account, is used to produce your
                    own results, and is never sold or used to advertise to you.
                  </p>
                </WhyDisclosure>

                <div className="row row-3 wrap">
                  <button
                    className="btn btn--primary"
                    disabled={saveProfile.isPending}
                    type="submit"
                  >
                    {saveProfile.isPending ? 'Saving…' : 'Save and continue'}
                  </button>
                </div>
              </form>
            ) : null}

            {/* =============================================== 2. income == */}
            {step === 1 ? (
              <div className="stack stack-6">
                <form className="stack stack-5" noValidate onSubmit={submitIncome}>
                  <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
                    Add each source of income you had in {taxYear}. Add as many
                    as you need — one row per slip or source keeps the later
                    breakdown readable.
                  </p>

                  <SelectField
                    id="income-type"
                    label="Kind of income"
                    onChange={(value) =>
                      setIncomeForm((previous) => ({ ...previous, type: value }))
                    }
                    options={INCOME_TYPES}
                    placeholder="Select a kind of income"
                    problem={problemFor(problems, 'income-type')}
                    value={incomeForm.type}
                  />

                  <AmountField
                    id="income-amount"
                    label={`Amount for ${taxYear}`}
                    onChange={(value) =>
                      setIncomeForm((previous) => ({ ...previous, amount: value }))
                    }
                    problem={problemFor(problems, 'income-amount')}
                    value={incomeForm.amount}
                  />

                  <TextField
                    hint="For example, the employer or institution named on the slip. It only helps you recognise the row later."
                    id="income-source"
                    label="Where it came from"
                    onChange={(value) =>
                      setIncomeForm((previous) => ({ ...previous, source: value }))
                    }
                    optional
                    value={incomeForm.source}
                  />

                  <WhyDisclosure id="income">
                    <p>
                      Income is what the whole estimate is built on: brackets,
                      the marginal rate and every credit that phases out are
                      decided by it. Without it Onyx has nothing to calculate.
                    </p>
                    <p style={{ marginTop: 'var(--space-2)' }}>
                      Amounts stay in your account and are used to produce your
                      own position. Onyx does not file anything on your behalf.
                    </p>
                  </WhyDisclosure>

                  <div>
                    <button
                      className="btn btn--secondary"
                      disabled={addIncome.isPending}
                      type="submit"
                    >
                      {addIncome.isPending ? 'Adding…' : 'Add this income'}
                    </button>
                  </div>
                </form>

                <div className="stack stack-3">
                  <h3 className="eyebrow">Income recorded for {taxYear}</h3>
                  <AsyncBlock query={income}>
                    {(rows) =>
                      rows.length === 0 ? (
                        <EmptyState
                          body={`Nothing has been recorded for ${taxYear} yet. Add your first source above.`}
                          title="No income yet"
                        />
                      ) : (
                        <div className="table-scroll">
                          <table className="data-table">
                            <caption className="sr-only">
                              Income sources recorded for tax year {taxYear}
                            </caption>
                            <thead>
                              <tr>
                                <th scope="col">Source</th>
                                <th className="numeric" scope="col">
                                  Amount
                                </th>
                              </tr>
                            </thead>
                            <tbody>
                              {rows.map((row) => (
                                <tr key={row.id}>
                                  <th scope="row">{row.source_name ?? 'Not named'}</th>
                                  <td className="numeric tabular">{money(row.amount)}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )
                    }
                  </AsyncBlock>
                </div>

                <StatedByYouNote />
              </div>
            ) : null}

            {/* =========================================== 3. registered == */}
            {step === 2 ? (
              <div className="stack stack-6">
                <form className="stack stack-5" noValidate onSubmit={submitRegistered}>
                  <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
                    This step is optional. Record contributions you have{' '}
                    <strong>already made</strong> in {taxYear} — money that is in
                    the account now. Contributions you are only thinking about
                    belong in the Decision Twin, where they can be modelled
                    without being treated as fact.
                  </p>

                  <SelectField
                    id="registered-type"
                    label="Account type"
                    onChange={(value) =>
                      setRegisteredForm((previous) => ({ ...previous, type: value }))
                    }
                    options={REGISTERED_TYPES}
                    placeholder="Select an account type"
                    problem={problemFor(problems, 'registered-type')}
                    value={registeredForm.type}
                  />

                  <AmountField
                    hint={`Total already contributed to this account in ${taxYear}. ${AMOUNT_GUIDANCE}`}
                    id="registered-contributions"
                    label="Contributed so far"
                    onChange={(value) =>
                      setRegisteredForm((previous) => ({ ...previous, contributions: value }))
                    }
                    problem={problemFor(problems, 'registered-contributions')}
                    value={registeredForm.contributions}
                  />

                  <AmountField
                    hint={`Only if you know it — the figure on your latest CRA notice of assessment. Leave it blank rather than guessing. ${AMOUNT_GUIDANCE}`}
                    id="registered-room"
                    label="Contribution room available"
                    onChange={(value) =>
                      setRegisteredForm((previous) => ({ ...previous, room: value }))
                    }
                    optional
                    problem={problemFor(problems, 'registered-room')}
                    value={registeredForm.room}
                  />

                  <WhyDisclosure id="registered">
                    <p>
                      A contribution already made changes your deduction this
                      year, so Onyx needs it to state your position correctly.
                      Room is asked for separately because Onyx cannot see your
                      CRA account: if you do not state it, no verified room
                      figure exists.
                    </p>
                    <p style={{ marginTop: 'var(--space-2)' }}>
                      These figures stay in your Onyx account and are not shared
                      with your financial institution or the CRA.
                    </p>
                  </WhyDisclosure>

                  <div>
                    <button
                      className="btn btn--secondary"
                      disabled={addAccount.isPending}
                      type="submit"
                    >
                      {addAccount.isPending ? 'Adding…' : 'Add this account'}
                    </button>
                  </div>
                </form>

                <div className="stack stack-4">
                  <h3 className="eyebrow">Registered contributions recorded for {taxYear}</h3>
                  <AsyncBlock query={registered}>
                    {(rows) =>
                      rows.length === 0 ? (
                        <EmptyState
                          body="Nothing recorded here. You can skip this step and add contributions later."
                          title="No registered contributions yet"
                        />
                      ) : (
                        <div className="stack stack-5">
                          {rows.map((row) => (
                            <div className="stack stack-3" key={row.asset_id}>
                              <div className="figrow figrow--3">
                                <div className="figrow__cell">
                                  <span className="figrow__label">Account</span>
                                  <span className="figure figure--sm">
                                    {row.registered_type}
                                  </span>
                                </div>
                                <div className="figrow__cell">
                                  <span className="figrow__label">Contributed</span>
                                  <span className="figure figure--sm tabular">
                                    {money(row.contributions_ytd)}
                                  </span>
                                </div>
                                <div className="figrow__cell">
                                  <span className="figrow__label">Room you stated</span>
                                  <span className="figure figure--sm tabular">
                                    {row.contribution_room === null
                                      ? 'Not stated'
                                      : money(row.contribution_room)}
                                  </span>
                                </div>
                              </div>
                              {/* Room provenance is never left implied. A figure
                                  the customer stated and a figure nobody has
                                  checked are different claims, and the note says
                                  which one this is. */}
                              <AssumptionNote
                                assumption={{
                                  assumption_code: 'CONTRIBUTION_ROOM_AVAILABLE',
                                  certainty:
                                    row.contribution_room === null
                                      ? 'platform_default'
                                      : 'user_asserted',
                                  note:
                                    row.contribution_room === null
                                      ? `You have not stated the room available on this ${row.registered_type}. Onyx has no verified figure for it, so anything that depends on room rests on an assumption — check your latest CRA notice of assessment before acting on it.`
                                      : `You stated ${money(row.contribution_room)} of room on this ${row.registered_type}. Onyx uses it as given and has not checked it against the CRA.`,
                                }}
                              />
                            </div>
                          ))}
                        </div>
                      )
                    }
                  </AsyncBlock>
                </div>

                <StatedByYouNote />
              </div>
            ) : null}

            {/* =============================================== 4. claims == */}
            {step === 3 ? (
              <div className="stack stack-6">
                <form className="stack stack-5" noValidate onSubmit={submitClaim}>
                  <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
                    This step is optional. Record amounts you paid in {taxYear}{' '}
                    that may reduce what you owe. Add one row per kind; you can
                    add several.
                  </p>

                  <SelectField
                    id="expense-category"
                    label="What the amount is for"
                    onChange={(value) =>
                      setClaimForm((previous) => ({ ...previous, category: value }))
                    }
                    options={EXPENSE_CATEGORIES}
                    placeholder="Select a category"
                    problem={problemFor(problems, 'expense-category')}
                    value={claimForm.category}
                  />

                  <AmountField
                    hint={`Total paid in ${taxYear} for this category. ${AMOUNT_GUIDANCE}`}
                    id="expense-amount"
                    label="Amount paid"
                    onChange={(value) =>
                      setClaimForm((previous) => ({ ...previous, amount: value }))
                    }
                    problem={problemFor(problems, 'expense-amount')}
                    value={claimForm.amount}
                  />

                  <TextField
                    hint="For example, the charity or clinic. It only helps you recognise the row later."
                    id="expense-description"
                    label="Description"
                    onChange={(value) =>
                      setClaimForm((previous) => ({ ...previous, description: value }))
                    }
                    optional
                    value={claimForm.description}
                  />

                  <WhyDisclosure id="claims">
                    <p>
                      Donations, medical costs, tuition and child care are the
                      amounts most often left out of an estimate, and each one
                      is governed by its own rules and thresholds. Onyx needs
                      the amounts before those rules can be applied to you.
                    </p>
                    <p style={{ marginTop: 'var(--space-2)' }}>
                      Medical amounts are health-related information. They stay
                      in your Onyx account, are used only for your own tax
                      results, and are never sold or used to advertise to you.
                    </p>
                  </WhyDisclosure>

                  <div>
                    <button
                      className="btn btn--secondary"
                      disabled={addClaim.isPending}
                      type="submit"
                    >
                      {addClaim.isPending ? 'Adding…' : 'Add this amount'}
                    </button>
                  </div>
                </form>

                <div className="stack stack-3">
                  <h3 className="eyebrow">Added in this visit</h3>
                  {claimsAdded.length === 0 ? (
                    <EmptyState
                      body="Nothing added yet. This step can be skipped, and amounts can be added later from your account."
                      title="No amounts added"
                    />
                  ) : (
                    <div className="table-scroll">
                      <table className="data-table">
                        <caption className="sr-only">
                          Donations, medical and other amounts added during this visit
                        </caption>
                        <thead>
                          <tr>
                            <th scope="col">Category</th>
                            <th scope="col">Description</th>
                            <th className="numeric" scope="col">
                              Amount
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {claimsAdded.map((row) => (
                            <tr key={row.id}>
                              <th scope="row">
                                {labelFor(EXPENSE_CATEGORIES, row.categoryCode)}
                              </th>
                              <td>{row.description || 'Not described'}</td>
                              <td className="numeric tabular">{money(row.amount)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>

                <StatedByYouNote />
              </div>
            ) : null}

            {/* =============================================== 5. review == */}
            {step === 4 ? (
              <div className="stack stack-6">
                <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
                  This is what Onyx has on file for {taxYear}. Running the
                  analysis produces your estimated position from these facts and
                  the governed tax data for the year. Nothing is filed, and you
                  can change any of it afterwards.
                </p>

                <div className="stack stack-3">
                  <h3 className="eyebrow">About you</h3>
                  <AsyncBlock query={profile}>
                    {(record) => (
                      <dl className="figrow figrow--3">
                        <div className="figrow__cell">
                          <dt className="figrow__label">Province</dt>
                          <dd className="figure figure--sm" style={{ margin: 0 }}>
                            {labelFor(PROVINCES, record.province_code)}
                          </dd>
                        </div>
                        <div className="figrow__cell">
                          <dt className="figrow__label">Marital status</dt>
                          <dd className="figure figure--sm" style={{ margin: 0 }}>
                            {labelFor(MARITAL_STATUSES, record.marital_status)}
                          </dd>
                        </div>
                        <div className="figrow__cell">
                          <dt className="figrow__label">Also recorded</dt>
                          <dd className="text-sm text-secondary" style={{ margin: 0 }}>
                            {[
                              record.is_self_employed ? 'Self-employed' : null,
                              record.is_student ? 'Student' : null,
                              record.has_rental_income ? 'Rental income' : null,
                            ]
                              .filter((entry): entry is string => entry !== null)
                              .join(' · ') || 'Nothing further'}
                          </dd>
                        </div>
                      </dl>
                    )}
                  </AsyncBlock>
                </div>

                <div className="stack stack-3">
                  <h3 className="eyebrow">Income</h3>
                  <AsyncBlock query={income}>
                    {(rows) => (
                      <p className="text-sm text-secondary">
                        {rows.length === 0
                          ? `No income is recorded for ${taxYear}. The analysis will still run, but it will have very little to work with.`
                          : `${rows.length} ${rows.length === 1 ? 'source' : 'sources'} recorded for ${taxYear}.`}
                      </p>
                    )}
                  </AsyncBlock>
                </div>

                <div className="stack stack-3">
                  <h3 className="eyebrow">Registered contributions</h3>
                  <AsyncBlock query={registered}>
                    {(rows) => (
                      <p className="text-sm text-secondary">
                        {rows.length === 0
                          ? 'No registered contributions recorded.'
                          : `${rows.length} ${rows.length === 1 ? 'account' : 'accounts'} recorded.`}
                      </p>
                    )}
                  </AsyncBlock>
                </div>

                <div className="stack stack-3">
                  <h3 className="eyebrow">Donations and medical</h3>
                  <p className="text-sm text-secondary">
                    {claimsAdded.length === 0
                      ? 'Nothing added in this visit.'
                      : `${claimsAdded.length} ${claimsAdded.length === 1 ? 'amount' : 'amounts'} added in this visit.`}
                  </p>
                </div>

                <div>
                  <button
                    className="btn btn--primary"
                    disabled={runAnalysis.isPending}
                    onClick={submitRun}
                    type="button"
                  >
                    {runAnalysis.isPending ? 'Running the analysis…' : 'Run my analysis'}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {/* Back is available from every step after the first, and the optional
            steps say so on the button rather than in fine print. The footer is
            omitted entirely on the first step so no empty bar is drawn. */}
        {step > 0 ? (
          <div className="panel__footer">
            <div className="row row-3 wrap">
              <button
                className="btn btn--ghost"
                onClick={() => goToStep(step - 1)}
                type="button"
              >
                Back
              </button>
              {step <= 3 ? (
                <button
                  className="btn btn--secondary"
                  onClick={() => goToStep(step + 1)}
                  type="button"
                >
                  {advanceLabel}
                </button>
              ) : null}
            </div>
          </div>
        ) : null}
      </section>
    </>
  )
}
