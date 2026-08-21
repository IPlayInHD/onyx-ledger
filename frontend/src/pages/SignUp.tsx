/* =========================================================================
   CREATE ACCOUNT  —  public surface
   =========================================================================
   The account itself is the only thing this screen collects. Tax facts are
   asked for afterwards, one at a time, where each one can be explained.

   Two decisions worth stating:

     · The password rule shown is EIGHT CHARACTERS, because that is the
       backend's own minimum. The screen never grades a password, never calls
       one strong, and never shows a meter — a meter is a judgement this
       product has no basis for, and customers act on it.
     · There is NO consent checkbox. The backend has no endpoint that stores
       consent, so a ticked box would create a legal record that does not
       exist anywhere. The agreement is stated in plain words with both
       documents linked, which is a claim the product can actually stand
       behind.
   ========================================================================= */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { useAuth } from '@/auth/AuthProvider'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { describeError } from '@/components/states'

type FieldName = 'email' | 'password'

/** Summary order is DOM order, so the list reads the way the form does. */
const FIELD_ORDER: readonly FieldName[] = ['email', 'password']

/* Permissive on purpose: this catches "no @ at all" and "no domain", which is
   every mistake a person actually makes typing an address. The backend's
   EmailStr is the authority on everything finer. */
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/** The registration minimum enforced by the backend's own schema. Checking it
 *  here saves a round trip; it does not replace the server's check. */
const PASSWORD_MIN_LENGTH = 8

type Problems = Partial<Record<FieldName, string>>

function validate(email: string, password: string): Problems {
  const problems: Problems = {}
  const trimmed = email.trim()
  if (!trimmed) problems.email = 'Enter your email address.'
  else if (!EMAIL_SHAPE.test(trimmed)) {
    problems.email = 'Enter an email address in the format name@example.com.'
  }
  if (!password) problems.password = 'Enter a password.'
  else if (password.length < PASSWORD_MIN_LENGTH) {
    problems.password = `Use a password of at least ${PASSWORD_MIN_LENGTH} characters.`
  }
  return problems
}

export default function SignUp() {
  const { register } = useAuth()
  const navigate = useNavigate()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Problems>({})
  const [formError, setFormError] = useState<{ title: string; body: string } | null>(null)
  const [pending, setPending] = useState(false)

  /* Incremented on every rejected attempt so a SECOND failure re-announces
     itself. Keying the effect on the error object alone would stay silent
     when the same message came back twice. */
  const [failureCount, setFailureCount] = useState(0)
  const summaryRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (failureCount > 0) summaryRef.current?.focus()
  }, [failureCount])

  const listedProblems = FIELD_ORDER.filter((name) => fieldErrors[name])
  const showSummary = formError !== null || listedProblems.length > 0

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const problems = validate(email, password)
    setFieldErrors(problems)
    setFormError(null)

    if (Object.keys(problems).length > 0) {
      setFailureCount((count) => count + 1)
      return
    }

    setPending(true)
    try {
      await register(email.trim(), password)
    } catch (error) {
      setPending(false)
      /* A taken address is a problem WITH A FIELD, so it is reported on the
         field and linked from the summary rather than as a detached banner.
         The backend must disclose this to register anyone at all; the screen
         says it in its own words and offers the way out. */
      if (error instanceof ApiError && error.isConflict) {
        setFieldErrors({
          email:
            'An account already exists for this email address. Sign in instead, or use another address.',
        })
      } else {
        setFormError(describeError(error))
      }
      setFailureCount((count) => count + 1)
      return
    }
    // `replace` so the back button does not return to a form that has already
    // been submitted successfully.
    navigate('/app/onboarding', { replace: true })
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <main className="shell__main" id="main">
        <div className="shell-container">
          <div className="measure-46 mx-auto">
            <div className="mb-6">
              <Wordmark to="/" />
            </div>

            <PageHead
              eyebrow="Account"
              title="Create your account"
              lede="An email address and a password is all Onyx needs to start. The tax facts come next, one step at a time."
            />

            <section className="panel">
              <div className="panel__body">
                {/* noValidate hands validation to this component so one set of
                    messages appears in one place. The `required` attributes
                    stay: they are what tells assistive technology the fields
                    are mandatory. */}
                <form className="stack stack-5" noValidate onSubmit={handleSubmit}>
                  {showSummary ? (
                    <div
                      className="error-summary"
                      ref={summaryRef}
                      role="alert"
                      tabIndex={-1}
                    >
                      <h2 className="text-sm mb-2">
                        {formError ? formError.title : 'Check what you entered'}
                      </h2>
                      {formError ? (
                        <p className="text-sm text-secondary">{formError.body}</p>
                      ) : null}
                      {listedProblems.length > 0 ? (
                        <ul className="pl-4">
                          {listedProblems.map((name) => (
                            <li className="text-sm" key={name}>
                              <a href={`#${name}`}>{fieldErrors[name]}</a>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </div>
                  ) : null}

                  <div className="field">
                    <label className="field__label" htmlFor="email">
                      Email address
                    </label>
                    <input
                      autoCapitalize="none"
                      autoComplete="email"
                      className="field__control"
                      id="email"
                      name="email"
                      onChange={(event) => setEmail(event.target.value)}
                      required
                      spellCheck={false}
                      type="email"
                      value={email}
                      aria-describedby={fieldErrors.email ? 'email-error' : undefined}
                      aria-invalid={fieldErrors.email ? true : undefined}
                    />
                    {fieldErrors.email ? (
                      <p className="field__error" id="email-error">
                        {fieldErrors.email}
                      </p>
                    ) : null}
                  </div>

                  <div className="field">
                    <label className="field__label" htmlFor="password">
                      Password
                    </label>
                    <input
                      autoComplete="new-password"
                      className="field__control"
                      id="password"
                      name="password"
                      onChange={(event) => setPassword(event.target.value)}
                      required
                      type="password"
                      value={password}
                      aria-describedby={
                        fieldErrors.password
                          ? 'password-hint password-error'
                          : 'password-hint'
                      }
                      aria-invalid={fieldErrors.password ? true : undefined}
                    />
                    <p className="field__hint" id="password-hint">
                      At least {PASSWORD_MIN_LENGTH} characters. A long phrase you
                      can remember works as well as anything.
                    </p>
                    {fieldErrors.password ? (
                      <p className="field__error" id="password-error">
                        {fieldErrors.password}
                      </p>
                    ) : null}
                  </div>

                  <div className="stack stack-4">
                    <p className="text-xs text-muted">
                      Creating an account means you agree to the{' '}
                      <Link to="/legal/terms">Terms of Service</Link> and the{' '}
                      <Link to="/legal/privacy">Privacy Policy</Link>.
                    </p>
                    <div>
                      <button className="btn btn--primary" disabled={pending} type="submit">
                        {pending ? 'Creating your account…' : 'Create account'}
                      </button>
                    </div>
                  </div>
                </form>
              </div>

              <div className="panel__footer">
                <p className="text-sm text-secondary">
                  Already have an account? <Link to="/sign-in">Sign in</Link>.
                </p>
              </div>
            </section>
          </div>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
