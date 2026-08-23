/* =========================================================================
   CHOOSE A NEW PASSWORD  —  public surface
   =========================================================================
   Reached from a link in an email, in whatever browser opened the mail.

   THE TOKEN IS HELD IN MEMORY AND NOWHERE ELSE. It is read out of the query
   string on mount, kept in component state for the one request that spends
   it, and the URL is rewritten immediately so it stops travelling in history,
   in a referrer, and in the screenshot somebody sends to support. It is never
   written to localStorage or sessionStorage, never logged, and never rendered
   — it is a credential that can set a password on somebody's tax account, and
   the fewer places it exists the better.

   A COMPLETED RESET DOES NOT SIGN ANYBODY IN. The backend answers 204 and
   grants nothing, deliberately: the premise of a reset is that someone else
   may have had access, so handing a session to whoever opened the link would
   be exactly the wrong reflex. The customer signs in with the password they
   just chose, which also proves they know it.
   ========================================================================= */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { recoveryApi } from '@/api/endpoints'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { describeError } from '@/components/states'

/* Mirrors the backend's `Field(min_length=8)`. A client-side floor that was
   LOWER would send a doomed request; one that was higher would refuse a
   password the product accepts. */
const MIN_LENGTH = 8

function describeResetFailure(error: unknown): { title: string; body: string } {
  if (error instanceof ApiError && error.isRecoveryLinkInvalid) {
    return {
      title: 'This link is no longer valid',
      body: 'Reset links work once and expire after an hour. Ask for a new one — your current password still works until you change it.',
    }
  }
  if (error instanceof ApiError && error.isValidation) {
    return {
      title: 'That password cannot be used',
      body: `Choose one of at least ${MIN_LENGTH} characters.`,
    }
  }
  return describeError(error)
}

export default function ResetPassword() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()

  /* Captured ONCE, from the first render's params, then the URL is cleared.
     Reading `params` on every render would find nothing after the rewrite. */
  const [token] = useState(() => params.get('token') ?? '')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [fieldErrors, setFieldErrors] = useState<{ password?: string; confirm?: string }>({})
  const [formError, setFormError] = useState<{ title: string; body: string } | null>(null)
  const [pending, setPending] = useState(false)
  const [done, setDone] = useState(false)

  const summaryRef = useRef<HTMLDivElement>(null)
  const [failureCount, setFailureCount] = useState(0)
  useEffect(() => {
    if (failureCount > 0) summaryRef.current?.focus()
  }, [failureCount])

  useEffect(() => {
    if (params.get('token')) setParams({}, { replace: true })
  }, [params, setParams])

  useEffect(() => {
    if (!done) return undefined
    const timer = setTimeout(() => navigate('/sign-in', { replace: true }), 1600)
    return () => clearTimeout(timer)
  }, [done, navigate])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)

    const problems: { password?: string; confirm?: string } = {}
    if (!password) problems.password = 'Choose a new password.'
    else if (password.length < MIN_LENGTH) {
      problems.password = `Use at least ${MIN_LENGTH} characters.`
    }
    // Confirmation is a TYPO GUARD, not security: the customer is about to be
    // signed out of everything, and a mistyped new password would lock them
    // out of an account they were in the middle of recovering.
    if (confirmation !== password) problems.confirm = 'Both passwords must match.'

    setFieldErrors(problems)
    if (Object.keys(problems).length > 0) {
      setFailureCount((count) => count + 1)
      return
    }

    setPending(true)
    try {
      await recoveryApi.completePasswordReset(token, password)
      setDone(true)
    } catch (error) {
      setFormError(describeResetFailure(error))
      setFailureCount((count) => count + 1)
    } finally {
      setPending(false)
    }
  }

  if (!token) {
    return (
      <div className="shell">
        <main className="shell__main" id="main">
          <div className="shell-container">
            <div className="measure-46 mx-auto">
              <div className="mb-6">
                <Wordmark to="/" />
              </div>
              <PageHead
                eyebrow="Account"
                title="This page needs a reset link"
                lede="Open the link from your email, or ask for a new one."
              />
              <section className="panel">
                <div className="panel__footer">
                  <p className="text-sm text-secondary">
                    <Link to="/forgot-password">Send a reset link</Link>
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

            {done ? (
              <>
                <PageHead
                  eyebrow="Account"
                  title="Your password is changed"
                  lede="Every device that was signed in has been signed out."
                />
                <section className="panel">
                  <div className="panel__body" role="status">
                    <div className="stack stack-4">
                      <p className="text-sm text-secondary">
                        Signing everything out is deliberate: if someone else
                        knew your old password, their session ends here too.
                      </p>
                      <p className="text-sm text-secondary">Taking you to sign in…</p>
                      <div>
                        <Link className="btn btn--primary" to="/sign-in">
                          Sign in
                        </Link>
                      </div>
                    </div>
                  </div>
                </section>
              </>
            ) : (
              <>
                <PageHead
                  eyebrow="Account"
                  title="Choose a new password"
                  lede="Your current password keeps working until you finish this."
                />
                <section className="panel">
                  <div className="panel__body">
                    <form className="stack stack-5" noValidate onSubmit={handleSubmit}>
                      {formError || Object.keys(fieldErrors).length > 0 ? (
                        <div className="error-summary" ref={summaryRef} role="alert" tabIndex={-1}>
                          <h2 className="text-sm mb-2">
                            {formError ? formError.title : 'Check what you entered'}
                          </h2>
                          {formError ? (
                            <p className="text-sm text-secondary">{formError.body}</p>
                          ) : null}
                          {Object.keys(fieldErrors).length > 0 ? (
                            <ul className="pl-4">
                              {fieldErrors.password ? (
                                <li className="text-sm">
                                  <a href="#new-password">{fieldErrors.password}</a>
                                </li>
                              ) : null}
                              {fieldErrors.confirm ? (
                                <li className="text-sm">
                                  <a href="#confirm-password">{fieldErrors.confirm}</a>
                                </li>
                              ) : null}
                            </ul>
                          ) : null}
                        </div>
                      ) : null}

                      <div className="field">
                        <label className="field__label" htmlFor="new-password">
                          New password
                        </label>
                        <input
                          autoComplete="new-password"
                          className="field__control"
                          id="new-password"
                          name="new-password"
                          onChange={(event) => setPassword(event.target.value)}
                          required
                          type="password"
                          value={password}
                          aria-describedby="new-password-hint"
                          aria-invalid={fieldErrors.password ? true : undefined}
                        />
                        <p className="field__hint" id="new-password-hint">
                          At least {MIN_LENGTH} characters.
                        </p>
                        {fieldErrors.password ? (
                          <p className="field__error">{fieldErrors.password}</p>
                        ) : null}
                      </div>

                      <div className="field">
                        <label className="field__label" htmlFor="confirm-password">
                          Confirm new password
                        </label>
                        <input
                          autoComplete="new-password"
                          className="field__control"
                          id="confirm-password"
                          name="confirm-password"
                          onChange={(event) => setConfirmation(event.target.value)}
                          required
                          type="password"
                          value={confirmation}
                          aria-invalid={fieldErrors.confirm ? true : undefined}
                        />
                        {fieldErrors.confirm ? (
                          <p className="field__error">{fieldErrors.confirm}</p>
                        ) : null}
                      </div>

                      <div>
                        <button className="btn btn--primary" disabled={pending} type="submit">
                          {pending ? 'Saving…' : 'Change password'}
                        </button>
                      </div>
                    </form>
                  </div>

                  <div className="panel__footer">
                    <p className="text-sm text-secondary">
                      Link expired? <Link to="/forgot-password">Ask for a new one</Link>.
                    </p>
                  </div>
                </section>
              </>
            )}
          </div>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
