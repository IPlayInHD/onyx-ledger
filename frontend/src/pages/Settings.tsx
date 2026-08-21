/* =========================================================================
   SETTINGS AND PRIVACY
   =========================================================================
   The account's own controls, and only the ones Onyx can actually honour.

   WHY THERE IS NO EXPORT BUTTON. There is no export endpoint. A "Download my
   data" control that opened a spinner and never finished would be a promise
   the product cannot keep, and in a privacy screen of all places that is the
   worst possible thing to ship. The screen offers deletion, which is real, and
   says nothing about what it cannot do.

   WHY THE SITUATION QUESTIONS ARE ALL ASKED. Saving a tax profile is a full
   replacement — every flag in the request is written. The API returns only
   three of the seven flags it accepts, so four of them cannot be pre-filled;
   submitting a form that silently sent `false` for those four would quietly
   erase facts that change which rules the engine examines. So the screen asks
   for all seven, says plainly why four start blank, and writes exactly what
   the customer confirmed.
   ========================================================================= */
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type RefObject,
} from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { PageHead } from '@/components/Shell'
import { ErrorState, LoadingBlock, describeError } from '@/components/states'
import { Provenance } from '@/components/trust'
import { isoDate } from '@/lib/format'
import {
  applyTheme,
  readStoredTheme,
  storeTheme,
  type ThemePreference,
} from '@/lib/theme'
import { keys, useProfile } from '@/lib/queries'
import { accountApi, profileApi, type TaxProfileIn } from '@/api/endpoints'
import { ApiError } from '@/api/client'
import { useAuth } from '@/auth/AuthProvider'

/* Account lifecycle is read on exactly one screen, so its key lives with the
   screen rather than in the shared key registry, which no page but this one
   would ever use. */
const DELETION_KEY = ['account', 'deletion'] as const

interface Option {
  value: string
  label: string
}

/** The four provinces the certified engine holds bracket and credit data for.
 *  Offering a fifth would produce a fail-closed error the customer cannot act
 *  on, so the other provinces are not offered rather than accepted and then
 *  refused. */
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

/* ---------------------------------------------------------- form problems -- */

interface Problem {
  field: string
  message: string
}

function problemFor(problems: readonly Problem[], field: string): string | undefined {
  return problems.find((problem) => problem.field === field)?.message
}

function ErrorSummary({
  error,
  problems,
  summaryRef,
  title,
}: {
  error: unknown
  problems: readonly Problem[]
  summaryRef: RefObject<HTMLDivElement | null>
  title: string
}) {
  const described = error ? describeError(error) : null
  if (problems.length === 0 && !described) return null
  return (
    <div className="error-summary" ref={summaryRef} role="alert" tabIndex={-1}>
      <h3 className="text-sm" style={{ marginBottom: 'var(--space-2)' }}>
        {described ? described.title : title}
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

/* ------------------------------------------------------------- controls -- */

function SelectField({
  id,
  label,
  hint,
  options,
  placeholder,
  value,
  onChange,
  problem,
}: {
  id: string
  label: string
  hint?: string
  options: readonly Option[]
  placeholder: string
  value: string
  onChange: (value: string) => void
  problem?: string | undefined
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

type YesNo = '' | 'yes' | 'no'

/** A yes/no question with no default. "Unanswered" is a real state here and it
 *  must not look like "no". */
function YesNoRow({
  id,
  label,
  hint,
  value,
  onChange,
  problem,
}: {
  id: string
  label: string
  hint?: string
  value: YesNo
  onChange: (value: YesNo) => void
  problem?: string | undefined
}) {
  const hintId = hint ? `${id}-hint` : null
  const errorId = problem ? `${id}-error` : null
  const describedBy = [hintId, errorId].filter(Boolean).join(' ')
  return (
    <div
      aria-describedby={describedBy || undefined}
      aria-labelledby={`${id}-label`}
      className="stack stack-2"
      id={id}
      role="radiogroup"
      style={{ padding: 'var(--space-3) var(--space-0)' }}
      tabIndex={-1}
    >
      <span className="field__label" id={`${id}-label`}>
        {label}
      </span>
      {hint && hintId ? (
        <span className="field__hint" id={hintId}>
          {hint}
        </span>
      ) : null}
      <div className="row row-4 wrap">
        {[
          { option: 'yes' as const, text: 'Yes' },
          { option: 'no' as const, text: 'No' },
        ].map(({ option, text }) => (
          <label
            className="row row-2"
            htmlFor={`${id}-${option}`}
            key={option}
            style={{ minHeight: '2.75rem', paddingRight: 'var(--space-3)' }}
          >
            <input
              aria-invalid={problem ? true : undefined}
              checked={value === option}
              id={`${id}-${option}`}
              name={id}
              onChange={() => onChange(option)}
              style={{
                width: 'var(--space-5)',
                height: 'var(--space-5)',
                flexShrink: 0,
              }}
              type="radio"
              value={option}
            />
            <span className="text-sm">{text}</span>
          </label>
        ))}
      </div>
      {problem && errorId ? (
        <p className="field__error" id={errorId}>
          {problem}
        </p>
      ) : null}
    </div>
  )
}

function Section({
  id,
  title,
  lede,
  footer,
  children,
}: {
  id: string
  title: string
  lede?: string
  footer?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="panel" aria-labelledby={`${id}-heading`}>
      <div className="panel__header">
        <div>
          <h2 className="section-title" id={`${id}-heading`}>
            {title}
          </h2>
          {lede ? (
            <p className="text-sm text-muted" style={{ marginTop: 'var(--space-1)' }}>
              {lede}
            </p>
          ) : null}
        </div>
      </div>
      <div className="panel__body">{children}</div>
      {footer ? <div className="panel__footer">{footer}</div> : null}
    </section>
  )
}

/* =========================================================================
   ACCOUNT
   ========================================================================= */

function AccountSection() {
  const { user, signOut } = useAuth()
  const navigate = useNavigate()

  return (
    <Section
      id="account"
      title="Account"
      lede="The address this account signs in with."
    >
      <div className="stack stack-5">
        <div className="field">
          <span className="field__label">Email address</span>
          <span className="text-sm">{user ? user.email : 'Not available'}</span>
          <span className="field__hint">
            Onyx signs you in with this address and uses it for nothing else.
            Changing it is not something this screen can do yet.
          </span>
        </div>

        <div>
          <button
            className="btn btn--secondary"
            onClick={() => {
              void signOut().then(() => navigate('/'))
            }}
            type="button"
          >
            Sign out
          </button>
        </div>
      </div>
    </Section>
  )
}

/* =========================================================================
   TAX PROFILE
   ========================================================================= */

interface ProfileForm {
  provinceCode: string
  maritalStatus: string
  isSelfEmployed: YesNo
  isStudent: YesNo
  hasRentalIncome: YesNo
  hasInvestments: YesNo
  ownsHome: YesNo
  firstTimeHomeBuyer: YesNo
  hasDisability: YesNo
}

const EMPTY_FORM: ProfileForm = {
  provinceCode: '',
  maritalStatus: '',
  isSelfEmployed: '',
  isStudent: '',
  hasRentalIncome: '',
  hasInvestments: '',
  ownsHome: '',
  firstTimeHomeBuyer: '',
  hasDisability: '',
}

/** Every situation question writes one boolean flag. Keeping the keys in one
 *  union is what lets the form update a flag without a cast. */
type FlagKey =
  | 'isSelfEmployed'
  | 'isStudent'
  | 'hasRentalIncome'
  | 'hasInvestments'
  | 'ownsHome'
  | 'firstTimeHomeBuyer'
  | 'hasDisability'

interface Question {
  key: FlagKey
  id: string
  label: string
  hint?: string
}

/** The three flags the API reads back, and the four it does not. The split is
 *  a fact about the contract, not a judgement about the questions. */
const RETURNED_QUESTIONS: readonly Question[] = [
  {
    key: 'isSelfEmployed',
    id: 'is-self-employed',
    label: 'Do you earn self-employment or business income?',
  },
  { key: 'isStudent', id: 'is-student', label: 'Are you a student?' },
  {
    key: 'hasRentalIncome',
    id: 'has-rental-income',
    label: 'Do you receive rental income?',
  },
]

const UNRETURNED_QUESTIONS: readonly Question[] = [
  {
    key: 'hasInvestments',
    id: 'has-investments',
    label: 'Do you hold investments outside a registered account?',
  },
  { key: 'ownsHome', id: 'owns-home', label: 'Do you own your home?' },
  {
    key: 'firstTimeHomeBuyer',
    id: 'first-time-home-buyer',
    label: 'Do you consider yourself a first-time home buyer?',
    hint: 'Onyx records this as you state it. The published rules decide what, if anything, it opens up.',
  },
  {
    key: 'hasDisability',
    id: 'has-disability',
    label: 'Do you have a disability?',
    hint: 'Answer yes only if you want Onyx to consider disability-related amounts. Answering no is fine.',
  },
]

function asBoolean(value: YesNo): boolean {
  return value === 'yes'
}

function fromBoolean(value: boolean): YesNo {
  return value ? 'yes' : 'no'
}

function TaxProfileSection() {
  const profile = useProfile()
  const queryClient = useQueryClient()

  const [form, setForm] = useState<ProfileForm>(EMPTY_FORM)
  const [problems, setProblems] = useState<Problem[]>([])
  const [saved, setSaved] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const summaryRef = useRef<HTMLDivElement>(null)

  const save = useMutation({
    mutationFn: profileApi.update,
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.profile })
      setProblems([])
      setSaved(true)
    },
    onError: () => setAttempt((count) => count + 1),
  })

  /* Seed from the record, and only from the record. The four flags the API
     does not return are deliberately left blank rather than defaulted to "no":
     a blank is honest about not knowing, a "no" would be a claim. */
  useEffect(() => {
    const record = profile.data
    if (!record) return
    setForm((previous) => ({
      ...previous,
      provinceCode: record.province_code ?? '',
      maritalStatus: record.marital_status ?? '',
      isSelfEmployed: fromBoolean(record.is_self_employed),
      isStudent: fromBoolean(record.is_student),
      hasRentalIncome: fromBoolean(record.has_rental_income),
    }))
  }, [profile.data])

  useEffect(() => {
    if (attempt > 0) summaryRef.current?.focus()
  }, [attempt])

  function change(patch: Partial<ProfileForm>) {
    setSaved(false)
    save.reset()
    setForm((previous) => ({ ...previous, ...patch }))
  }

  function changeFlag(key: FlagKey, value: YesNo) {
    setSaved(false)
    save.reset()
    setForm((previous) => {
      const next: ProfileForm = { ...previous }
      next[key] = value
      return next
    })
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found: Problem[] = []
    if (!form.provinceCode) {
      found.push({ field: 'province', message: 'Choose the province you file in.' })
    }
    if (!form.maritalStatus) {
      found.push({ field: 'marital-status', message: 'Choose your marital status.' })
    }
    for (const question of [...RETURNED_QUESTIONS, ...UNRETURNED_QUESTIONS]) {
      if (!form[question.key]) {
        found.push({
          field: question.id,
          message: `Answer yes or no: ${question.label}`,
        })
      }
    }
    if (found.length > 0) {
      setSaved(false)
      setProblems(found)
      setAttempt((count) => count + 1)
      return
    }

    setProblems([])
    const body: TaxProfileIn = {
      province_code: form.provinceCode,
      marital_status: form.maritalStatus,
      is_self_employed: asBoolean(form.isSelfEmployed),
      is_student: asBoolean(form.isStudent),
      has_rental_income: asBoolean(form.hasRentalIncome),
      has_investments: asBoolean(form.hasInvestments),
      owns_home: asBoolean(form.ownsHome),
      first_time_home_buyer: asBoolean(form.firstTimeHomeBuyer),
      has_disability: asBoolean(form.hasDisability),
    }
    save.mutate(body)
  }

  const notYetSet =
    profile.isError && profile.error instanceof ApiError && profile.error.isNotFound

  return (
    <Section
      id="tax-profile"
      title="Tax profile"
      lede="The facts the engine cannot work without, and the situations that decide which rules it examines."
      footer={
        <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
          Changing a fact the tax engine reads can mark results produced before
          the change as out of date. Onyx says so on the screens that show them
          rather than quietly recalculating behind you.
        </p>
      }
    >
      {profile.isPending ? (
        <LoadingBlock label="Loading your tax profile" />
      ) : profile.isError && !notYetSet ? (
        <ErrorState error={profile.error} onRetry={profile.refetch} />
      ) : (
        <form className="stack stack-5" noValidate onSubmit={submit}>
          <ErrorSummary
            error={save.error}
            problems={problems}
            summaryRef={summaryRef}
            title="Check what you entered"
          />

          {notYetSet ? (
            <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
              You have not set a tax profile yet. Filling this in gives the
              engine the province and status it needs before it can produce a
              position.
            </p>
          ) : null}

          <SelectField
            hint="Onyx supports Alberta, British Columbia and Ontario today. Quebec files a separate provincial return with its own pension and parental-insurance contributions, which Onyx does not calculate yet, so it is not offered rather than accepted and answered with a wrong figure."
            id="province"
            label="Province you file in"
            onChange={(value) => change({ provinceCode: value })}
            options={PROVINCES}
            placeholder="Select a province"
            problem={problemFor(problems, 'province')}
            value={form.provinceCode}
          />

          <SelectField
            id="marital-status"
            label="Marital status"
            onChange={(value) => change({ maritalStatus: value })}
            options={MARITAL_STATUSES}
            placeholder="Select your marital status"
            problem={problemFor(problems, 'marital-status')}
            value={form.maritalStatus}
          />

          <div className="stack stack-2">
            <span className="eyebrow">Your situation</span>
            <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
              Each answer only widens or narrows which published rules Onyx
              examines. Nothing here is checked against an outside source —
              Onyx records what you state.
            </p>
          </div>

          <div className="stack">
            {RETURNED_QUESTIONS.map((question) => (
              <YesNoRow
                hint={question.hint}
                id={question.id}
                key={question.key}
                label={question.label}
                onChange={(value) => changeFlag(question.key, value)}
                problem={problemFor(problems, question.id)}
                value={form[question.key]}
              />
            ))}
          </div>

          <div className="stack stack-3">
            <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
              The four questions below start blank every time. Onyx stores your
              answers but does not send them back to this screen, so it cannot
              show you what you last said — and saving records exactly what is
              answered here. Leaving one blank is not treated as a no.
            </p>
            <div className="stack">
              {UNRETURNED_QUESTIONS.map((question) => (
                <YesNoRow
                  hint={question.hint}
                  id={question.id}
                  key={question.key}
                  label={question.label}
                  onChange={(value) => changeFlag(question.key, value)}
                  problem={problemFor(problems, question.id)}
                  value={form[question.key]}
                />
              ))}
            </div>
          </div>

          <div className="row row-4 wrap">
            <button className="btn btn--primary" disabled={save.isPending} type="submit">
              {save.isPending ? 'Saving…' : 'Save tax profile'}
            </button>
            <Provenance kind="user" />
          </div>

          <p aria-live="polite" className="text-sm" role="status">
            {saved ? 'Your tax profile was saved.' : ''}
          </p>
        </form>
      )}
    </Section>
  )
}

/* =========================================================================
   PRIVACY
   ========================================================================= */

function PrivacySection() {
  return (
    <Section
      id="privacy"
      title="Privacy"
      lede="What Onyx keeps, and why it keeps it."
    >
      <div className="stack stack-4" style={{ maxWidth: '68ch' }}>
        <p className="text-sm text-secondary">
          Onyx stores the facts you give it — your province and status, your
          income and account entries, the documents you have supplied — and the
          results it calculates from them. It also keeps a record of what it
          showed you and when, which is what lets it tell you later that a
          figure has gone out of date instead of quietly changing it.
        </p>
        <p className="text-sm text-secondary">
          That information is used to produce your own results and to show their
          working. It is not sold, and it is not used to advertise to you. When
          Onyx puts a result into words with a language model, it sends the
          figures it had already calculated — not your name, not your email
          address, and not your documents.
        </p>
        <div className="row row-4 wrap">
          <Link className="btn btn--secondary" to="/legal/privacy">
            Read the Privacy Policy
          </Link>
          <Link className="btn btn--ghost" to="/trust">
            Trust Centre
          </Link>
        </div>
      </div>
    </Section>
  )
}

/* =========================================================================
   DATA CONTROLS
   ========================================================================= */

interface DeletionState {
  status: string
  requestedAt: string | null
}

/** The account endpoint is documented as a deliberately tiny, untyped object.
 *  Read it defensively and fall back to saying nothing rather than to saying
 *  "active", which would be the one answer a deleting customer must not see. */
function readDeletionState(payload: Record<string, unknown> | undefined): DeletionState {
  const status = typeof payload?.['status'] === 'string' ? payload['status'] : 'unknown'
  const requestedAt =
    typeof payload?.['requested_at'] === 'string' ? payload['requested_at'] : null
  return { status, requestedAt }
}

const DELETION_COPY: Record<string, { tone: string; label: string; body: string }> = {
  active: {
    tone: 'ready',
    label: 'Active',
    body: 'No deletion has been requested for this account.',
  },
  deletion_requested: {
    tone: 'blocked',
    label: 'Deletion requested',
    body: 'This account is being deleted. It can no longer be used, and the request cannot be withdrawn from this screen.',
  },
  deletion_complete: {
    tone: 'neutral',
    label: 'Deletion complete',
    body: 'Onyx has finished removing this account.',
  },
  unknown: {
    tone: 'neutral',
    label: 'Not established',
    body: 'Onyx could not read the current state of this account. Nothing has been changed.',
  },
}

function deletionCopy(status: string) {
  return DELETION_COPY[status] ?? DELETION_COPY['unknown']!
}

function DataControlsSection() {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const [typed, setTyped] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  const deletion = useQuery({
    queryKey: DELETION_KEY,
    queryFn: accountApi.deletionStatus,
  })

  const request = useMutation({
    /* One request id per attempt, generated at call time: the backend treats
       the request as idempotent, and a fresh id per click is what makes a
       second click a second intent rather than a replay. */
    mutationFn: () => accountApi.requestDeletion(crypto.randomUUID()),
    retry: false,
    onSuccess: () => {
      setConfirming(false)
      setTyped('')
      void queryClient.invalidateQueries({ queryKey: DELETION_KEY })
    },
  })

  const state = readDeletionState(deletion.data)
  const copy = deletionCopy(state.status)
  const inProgress =
    state.status === 'deletion_requested' || state.status === 'deletion_complete'

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (typed.trim() !== 'DELETE') {
      setProblem('Type DELETE in capitals to confirm. Nothing has been deleted.')
      return
    }
    setProblem(null)
    request.mutate()
  }

  return (
    <Section
      id="data-controls"
      title="Data controls"
      lede="Deleting your account is the one data control Onyx can carry out today."
      footer={
        <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
          There is no export, download or correction button here because there
          is no endpoint behind one. A control that cannot be honoured is a
          false promise, and this screen would rather be short than misleading.
        </p>
      }
    >
      <div className="stack stack-5">
        <div className="stack stack-3">
          <span className="eyebrow">Account state</span>
          {deletion.isPending ? (
            <LoadingBlock label="Checking your account state" />
          ) : deletion.isError ? (
            <ErrorState error={deletion.error} onRetry={deletion.refetch} />
          ) : (
            <div className="stack stack-2">
              <div className="row row-3 wrap">
                <span className={`status status--${copy.tone}`}>{copy.label}</span>
                {state.requestedAt ? (
                  <span className="text-xs text-muted">
                    Requested on {isoDate(state.requestedAt)}
                  </span>
                ) : null}
              </div>
              <p className="text-sm text-secondary" style={{ maxWidth: '62ch' }}>
                {copy.body}
              </p>
            </div>
          )}
        </div>

        {/* No delete control until the current state has actually been read.
            Offering an irreversible action while the account's own state is
            unknown is how a customer deletes something twice, or deletes it
            while a request is already running. */}
        {deletion.isPending || deletion.isError || inProgress ? null : (
          <div className="stack stack-4">
            <div className="stack stack-3" style={{ maxWidth: '62ch' }}>
              <h3 className="text-sm">Delete this account</h3>
              <p className="text-sm text-secondary">
                Requesting deletion is irreversible. This is what happens:
              </p>
              <ul className="stack stack-2" style={{ paddingLeft: 'var(--space-5)' }}>
                <li className="text-sm text-secondary">
                  Your access ends straight away. Onyx will refuse everything
                  except a check on the state of the deletion itself.
                </li>
                <li className="text-sm text-secondary">
                  Your tax profile, financial entries, analyses, scenarios and
                  decision journals are scheduled for permanent removal.
                </li>
                <li className="text-sm text-secondary">
                  Onyx reports the deletion as complete only once the removal has
                  actually run, so this screen will say “requested” until then.
                </li>
                <li className="text-sm text-secondary">
                  Nothing here can undo it, and Onyx cannot restore the account
                  afterwards.
                </li>
              </ul>
            </div>

            {!confirming ? (
              <div>
                <button
                  className="btn btn--secondary"
                  onClick={() => {
                    setProblem(null)
                    setConfirming(true)
                  }}
                  type="button"
                >
                  Delete my account
                </button>
              </div>
            ) : (
              <form className="stack stack-4" noValidate onSubmit={submit}>
                {request.isError ? <ErrorState error={request.error} /> : null}

                <div className="field" style={{ maxWidth: '24rem' }}>
                  <label className="field__label" htmlFor="confirm-delete">
                    Type DELETE to confirm
                  </label>
                  <input
                    aria-describedby={
                      problem ? 'confirm-delete-error confirm-delete-hint' : 'confirm-delete-hint'
                    }
                    aria-invalid={problem ? true : undefined}
                    autoComplete="off"
                    className="field__control"
                    id="confirm-delete"
                    onChange={(event) => {
                      setProblem(null)
                      setTyped(event.target.value)
                    }}
                    type="text"
                    value={typed}
                  />
                  <p className="field__hint" id="confirm-delete-hint">
                    Capitals, exactly: DELETE. This step exists so the account is
                    never deleted by a mis-click.
                  </p>
                  {problem ? (
                    <p className="field__error" id="confirm-delete-error">
                      {problem}
                    </p>
                  ) : null}
                </div>

                <div className="row row-4 wrap">
                  <button
                    className="btn btn--primary"
                    disabled={request.isPending}
                    type="submit"
                  >
                    {request.isPending ? 'Requesting…' : 'Permanently delete my account'}
                  </button>
                  <button
                    className="btn btn--ghost"
                    disabled={request.isPending}
                    onClick={() => {
                      setConfirming(false)
                      setTyped('')
                      setProblem(null)
                      request.reset()
                    }}
                    type="button"
                  >
                    Keep my account
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
      </div>
    </Section>
  )
}

/* =========================================================================
   APPEARANCE
   ========================================================================= */

const THEME_OPTIONS: readonly { value: ThemePreference; label: string; hint: string }[] = [
  {
    value: 'system',
    label: 'Match my system',
    hint: 'Follows whatever your device is set to, including a schedule.',
  },
  { value: 'light', label: 'Light', hint: 'Always the light palette.' },
  { value: 'dark', label: 'Dark', hint: 'Always the dark palette.' },
]

function AppearanceSection() {
  const [theme, setTheme] = useState<ThemePreference>(readStoredTheme)

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  return (
    <Section
      id="appearance"
      title="Appearance"
      lede="Light, dark, or whatever this device is already set to."
      footer={
        <p className="text-xs text-muted" style={{ maxWidth: '68ch' }}>
          This preference is kept in this browser only. It is a display setting,
          so it never reaches Onyx and is not part of your account.
        </p>
      }
    >
      <div
        aria-labelledby="theme-label"
        className="stack"
        role="radiogroup"
        id="theme"
      >
        <span className="field__label" id="theme-label">
          Colour theme
        </span>
        {THEME_OPTIONS.map((option) => (
          <label
            className="row row-3"
            htmlFor={`theme-${option.value}`}
            key={option.value}
            style={{
              padding: 'var(--space-3) var(--space-0)',
              minHeight: '2.75rem',
              alignItems: 'flex-start',
            }}
          >
            <input
              aria-describedby={`theme-${option.value}-hint`}
              checked={theme === option.value}
              id={`theme-${option.value}`}
              name="theme"
              onChange={() => {
                setTheme(option.value)
                storeTheme(option.value)
              }}
              style={{
                width: 'var(--space-5)',
                height: 'var(--space-5)',
                flexShrink: 0,
                marginTop: 'var(--space-1)',
              }}
              type="radio"
              value={option.value}
            />
            <span className="stack stack-2">
              <span className="text-sm">{option.label}</span>
              <span className="field__hint" id={`theme-${option.value}-hint`}>
                {option.hint}
              </span>
            </span>
          </label>
        ))}
      </div>
    </Section>
  )
}

/* =========================================================================
   SCREEN
   ========================================================================= */

export default function Settings() {
  return (
    <>
      <PageHead
        eyebrow="Your account"
        title="Settings and privacy"
        lede="Who Onyx thinks you are, what it keeps about you, and how to end it."
      />

      <div className="stack stack-6">
        <AccountSection />
        <TaxProfileSection />
        <PrivacySection />
        <DataControlsSection />
        <AppearanceSection />
      </div>
    </>
  )
}
