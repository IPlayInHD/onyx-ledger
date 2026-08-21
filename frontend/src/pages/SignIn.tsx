/* =========================================================================
   SIGN IN  —  public surface
   =========================================================================
   Two fields and one decision, so everything interesting here is about how
   the screen behaves when it goes wrong:

     · shape validation only. The browser never tells someone their password
       is "too short" for an account that already exists — that is the
       server's call, and a length hint on this form would be a free clue.
     · ONE generic failure. The backend answers a bad email and a bad password
       identically on purpose; this screen must not undo that by phrasing them
       differently, so the 401 copy is written here rather than taken from
       describeError(), whose 401 wording is about an expired session.
     · the error summary takes focus, is linked to the offending field, and
       never relies on the red border to carry the message.

   No AppShell: a visitor here is not signed in, so there is no year context,
   no navigation rail and no account menu to render.
   ========================================================================= */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { useAuth } from '@/auth/AuthProvider'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { describeError } from '@/components/states'

type FieldName = 'email' | 'password'

/** Summary order is DOM order, so the list reads the way the form does. */
const FIELD_ORDER: readonly FieldName[] = ['email', 'password']

/* Deliberately permissive: this catches "no @ at all" and "no domain", which
   is every mistake a person actually makes typing an address. Anything
   stricter starts rejecting valid addresses, and the backend's EmailStr is
   the authority on the rest. */
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

type Problems = Partial<Record<FieldName, string>>

function validate(email: string, password: string): Problems {
  const problems: Problems = {}
  const trimmed = email.trim()
  if (!trimmed) problems.email = 'Enter your email address.'
  else if (!EMAIL_SHAPE.test(trimmed)) {
    problems.email = 'Enter an email address in the format name@example.com.'
  }
  // Presence only. A minimum length here would be a hint about the stored
  // credential, and it would reject nobody the server would have let in.
  if (!password) problems.password = 'Enter your password.'
  return problems
}

/**
 * A refused sign-in is NOT the same event as a session that ran out, so it
 * does not get describeError()'s 401 copy. Everything else — throttling,
 * server faults, a dropped connection — is already described correctly there.
 */
function describeSignInFailure(error: unknown): { title: string; body: string } {
  if (error instanceof ApiError && error.isUnauthorized) {
    return {
      title: 'That email and password do not match',
      body: 'Check both and try again. Onyx does not say which of the two was wrong, because that would let anyone use this form to find out whether an address has an account.',
    }
  }
  return describeError(error)
}

/**
 * Where to land after signing in. `RequireAuth` puts the blocked path in
 * router state; anything else is ignored. Only an in-app absolute path is
 * accepted — a value starting `//` is a protocol-relative URL and would send
 * the customer off-site immediately after they typed a password.
 */
function redirectTarget(state: unknown): string {
  if (state && typeof state === 'object' && 'from' in state) {
    const from = (state as { from?: unknown }).from
    if (typeof from === 'string' && from.startsWith('/') && !from.startsWith('//')) {
      return from
    }
  }
  return '/app'
}

export default function SignIn() {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

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
      await signIn(email.trim(), password)
    } catch (error) {
      setPending(false)
      setFormError(describeSignInFailure(error))
      setFailureCount((count) => count + 1)
      return
    }
    // `replace` so the back button does not return to a form the customer has
    // already passed through.
    navigate(redirectTarget(location.state), { replace: true })
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
              title="Sign in"
              lede="Use the email address and password you registered with."
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
                      autoComplete="current-password"
                      className="field__control"
                      id="password"
                      name="password"
                      onChange={(event) => setPassword(event.target.value)}
                      required
                      type="password"
                      value={password}
                      aria-describedby={fieldErrors.password ? 'password-error' : undefined}
                      aria-invalid={fieldErrors.password ? true : undefined}
                    />
                    {fieldErrors.password ? (
                      <p className="field__error" id="password-error">
                        {fieldErrors.password}
                      </p>
                    ) : null}
                  </div>

                  <div>
                    <button className="btn btn--primary" disabled={pending} type="submit">
                      {pending ? 'Signing in…' : 'Sign in'}
                    </button>
                  </div>
                </form>
              </div>

              <div className="panel__footer">
                <div className="stack stack-2">
                  <p className="text-sm text-secondary">
                    New to Onyx? <Link to="/sign-up">Create an account</Link>.
                  </p>
                  {/* Said plainly rather than hidden behind a link that would
                      go nowhere: the certified backend exposes no password
                      reset, so this screen will not pretend to offer one. */}
                  <p className="text-xs text-muted">
                    Onyx cannot yet send a password reset email. Keep your
                    password somewhere you can retrieve it.
                  </p>
                </div>
              </div>
            </section>
          </div>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
