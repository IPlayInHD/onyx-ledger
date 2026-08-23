/* =========================================================================
   FORGOT PASSWORD  —  public surface
   =========================================================================
   ONE ANSWER FOR EVERY ADDRESS, and that is the entire design.

   The backend answers 202 with identical copy whether the address has an
   account, has a suspended one, is mid-deletion, or has never been seen — so
   this screen must not undo that by rendering the outcome differently. There
   is deliberately no branch here on anything but "did the request itself
   fail": success renders one fixed panel, and a customer who mistyped their
   address sees exactly what a customer who typed it correctly sees.

   That is worse UX than "no account found", and it is the right trade. This
   form is unauthenticated and rate-limited rather than secret, so a version
   that confirmed which addresses were real would be a list of Onyx customers
   available to anybody with a script.
   ========================================================================= */
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { recoveryApi } from '@/api/endpoints'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { describeError } from '@/components/states'

/* The same permissive shape SignIn uses: catches "no @" and "no domain",
   which is every mistake a person actually makes, and rejects nothing valid.
   The backend's EmailStr is the authority on the rest. */
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export default function ForgotPassword() {
  const [email, setEmail] = useState('')
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [formError, setFormError] = useState<{ title: string; body: string } | null>(null)
  const [pending, setPending] = useState(false)
  const [sent, setSent] = useState(false)

  const announcementRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (sent) announcementRef.current?.focus()
  }, [sent])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = email.trim()
    setFormError(null)

    if (!trimmed) {
      setFieldError('Enter your email address.')
      return
    }
    if (!EMAIL_SHAPE.test(trimmed)) {
      setFieldError('Enter an email address in the format name@example.com.')
      return
    }
    setFieldError(null)
    setPending(true)
    try {
      await recoveryApi.requestPasswordReset(trimmed)
      // Deliberately does not read the response. The body is a fixed string by
      // contract; rendering it would invite a future version that renders
      // something conditional.
      setSent(true)
    } catch (error) {
      setFormError(describeError(error))
    } finally {
      setPending(false)
    }
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

            {sent ? (
              <>
                <PageHead
                  eyebrow="Account"
                  title="Check your inbox"
                  lede="If that address has an Onyx account, a reset link is on its way."
                />
                <section className="panel">
                  <div className="panel__body" ref={announcementRef} role="status" tabIndex={-1}>
                    <div className="stack stack-4">
                      <p className="text-sm text-secondary">
                        The link works once and expires in an hour. Your current
                        password keeps working until you choose a new one.
                      </p>
                      <p className="text-sm text-secondary">
                        Nothing arrived? Check spam first. Onyx does not say
                        whether an address has an account — that would let
                        anyone use this form to find out who its customers are —
                        so a missing message may simply mean a different address
                        was used to register.
                      </p>
                    </div>
                  </div>
                  <div className="panel__footer">
                    <p className="text-sm text-secondary">
                      <Link to="/sign-in">Back to sign in</Link>
                    </p>
                  </div>
                </section>
              </>
            ) : (
              <>
                <PageHead
                  eyebrow="Account"
                  title="Reset your password"
                  lede="Tell us the address you registered with and we will send a link."
                />
                <section className="panel">
                  <div className="panel__body">
                    <form className="stack stack-5" noValidate onSubmit={handleSubmit}>
                      {formError ? (
                        <div className="error-summary" role="alert">
                          <h2 className="text-sm mb-2">{formError.title}</h2>
                          <p className="text-sm text-secondary">{formError.body}</p>
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
                          aria-describedby={fieldError ? 'email-error' : undefined}
                          aria-invalid={fieldError ? true : undefined}
                        />
                        {fieldError ? (
                          <p className="field__error" id="email-error">
                            {fieldError}
                          </p>
                        ) : null}
                      </div>

                      <div>
                        <button className="btn btn--primary" disabled={pending} type="submit">
                          {pending ? 'Sending…' : 'Send reset link'}
                        </button>
                      </div>
                    </form>
                  </div>

                  <div className="panel__footer">
                    <p className="text-sm text-secondary">
                      Remembered it? <Link to="/sign-in">Sign in</Link>.
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
