/* =========================================================================
   CONFIRM YOUR EMAIL ADDRESS  —  public surface, two jobs
   =========================================================================
   ONE SCREEN, TWO STATES, and they are one component because they are one
   place in the customer's head:

     · no ?token  — "we sent you a link", with a way to send another. Reached
                    by RequireAuth when a signed-in account has not confirmed
                    its address.
     · ?token=…   — redeeming the link, reached from the email itself, in
                    whatever browser opened it. Usually not the one holding
                    the session, which is why redemption is anonymous.

   WHAT THIS SCREEN NEVER SHOWS. The token, in any state: not in copy, not in
   an error, not in a heading. It is a single-use credential that can activate
   an account, and the URL it arrives in is already the most exposed place it
   will ever be — in history, in a referrer, in a screenshot of a support
   ticket. Its only job here is to be spent and forgotten, so the query string
   is stripped from the URL the moment it is read.

   Expired, already used, tampered with and never real are ONE message, because
   the backend deliberately answers them identically: telling the holder of a
   guessed token that it "expired" tells them they guessed a real one.
   ========================================================================= */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { recoveryApi } from '@/api/endpoints'
import { useAuth } from '@/auth/AuthProvider'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { describeError } from '@/components/states'

type Redemption = 'idle' | 'working' | 'done' | 'failed'

function describeRedemptionFailure(error: unknown): { title: string; body: string } {
  if (error instanceof ApiError && error.isRecoveryLinkInvalid) {
    return {
      title: 'This link is no longer valid',
      body: 'Verification links work once and expire. Sign in and ask for a new one — it takes a moment and the old link stops working.',
    }
  }
  return describeError(error)
}

function describeResendFailure(error: unknown): { title: string; body: string } {
  if (error instanceof ApiError && error.isRecoveryThrottled) {
    const wait = error.retryAfterSeconds
    return {
      title: 'A message is already on its way',
      body: wait
        ? `Check your inbox, including spam. You can ask for another in ${wait} seconds.`
        : 'Check your inbox, including spam, before asking for another.',
    }
  }
  return describeError(error)
}

export default function VerifyEmail() {
  const { status, pendingEmail, resendVerification, recheck, signOut } = useAuth()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()

  const token = params.get('token')

  const [redemption, setRedemption] = useState<Redemption>(token ? 'working' : 'idle')
  const [redemptionError, setRedemptionError] =
    useState<{ title: string; body: string } | null>(null)
  const [resendState, setResendState] = useState<'idle' | 'sending' | 'sent'>('idle')
  const [resendError, setResendError] = useState<{ title: string; body: string } | null>(null)

  /* Announced rather than merely rendered: someone using a screen reader is
     otherwise told nothing when the page finishes doing the only thing it
     came here to do. */
  const announcementRef = useRef<HTMLDivElement>(null)

  /* React 18 mounts effects twice in development StrictMode. A verification
     token is SINGLE USE, so a second redemption would consume the customer's
     link and then report it invalid — a bug that appears only in the dev
     server and looks exactly like a broken backend. */
  const redeemed = useRef(false)

  useEffect(() => {
    if (!token || redeemed.current) return
    redeemed.current = true

    // Strip the token from the address bar before doing anything else, so it
    // is out of the URL a customer might copy, bookmark or screenshot.
    setParams({}, { replace: true })

    void (async () => {
      try {
        await recoveryApi.confirmVerification(token)
        setRedemption('done')
        // If this tab also holds the session, open the product without making
        // them sign in again. If it does not, `recheck` finds no tokens and
        // the screen just shows its success state with a sign-in link.
        try {
          await recheck()
        } catch {
          /* Not the customer's problem: the address IS confirmed. */
        }
      } catch (error) {
        setRedemption('failed')
        setRedemptionError(describeRedemptionFailure(error))
      }
    })()
  }, [token, setParams, recheck])

  useEffect(() => {
    if (redemption === 'done' || redemption === 'failed') {
      announcementRef.current?.focus()
    }
  }, [redemption])

  /* DOES THIS TAB HOLD A SESSION? That is the only question this screen has to
     answer, and it is deliberately not the same question as "may they use the
     product". `legal-outstanding` is signed in — B4 added a second gate after
     this one, and reading only `authenticated` here left a customer who had
     just confirmed their address stranded on this page, being told they had
     used a different browser. They had not. Caught by the E2E suite, and the
     reason the check is a session question rather than a status equality. */
  const holdsSession = status === 'authenticated' || status === 'legal-outstanding'

  /* Verified and holding a session in this tab: there is nothing left to do
     here, so go where they were going.

     `/app` ON PURPOSE, even when the legal gate is outstanding. `RequireAuth`
     is the ONE place that maps a session status onto a destination; sending
     this screen straight to `/legal/accept` would be a second copy of that map
     and the two would drift the first time a gate is added or removed. The
     redirect happens inside the router, so nothing of the product renders. */
  useEffect(() => {
    if (redemption === 'done' && holdsSession) {
      const timer = setTimeout(() => navigate('/app', { replace: true }), 1200)
      return () => clearTimeout(timer)
    }
    return undefined
  }, [redemption, holdsSession, navigate])

  const handleResend = useCallback(async () => {
    setResendError(null)
    setResendState('sending')
    try {
      await resendVerification()
      setResendState('sent')
    } catch (error) {
      setResendState('idle')
      setResendError(describeResendFailure(error))
    }
  }, [resendVerification])

  const addressed = pendingEmail ? <strong>{pendingEmail}</strong> : 'your email address'

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

            {redemption === 'working' ? (
              <>
                <PageHead
                  eyebrow="Account"
                  title="Confirming your email address"
                  lede="This takes a moment."
                />
                <section className="panel">
                  <div className="panel__body">
                    <p className="text-sm text-secondary">Checking your link…</p>
                  </div>
                </section>
              </>
            ) : null}

            {redemption === 'done' ? (
              <>
                <PageHead
                  eyebrow="Account"
                  title="Your email address is confirmed"
                  lede="That is the last of the setup."
                />
                <section className="panel">
                  <div className="panel__body" ref={announcementRef} role="status" tabIndex={-1}>
                    {holdsSession ? (
                      <p className="text-sm text-secondary">
                        Taking you to Onyx…
                      </p>
                    ) : (
                      <div className="stack stack-4">
                        <p className="text-sm text-secondary">
                          You confirmed it in a different browser from the one
                          you signed up in, which is completely normal. Sign in
                          to pick up where you left off.
                        </p>
                        <div>
                          <Link className="btn btn--primary" to="/sign-in">
                            Sign in
                          </Link>
                        </div>
                      </div>
                    )}
                  </div>
                </section>
              </>
            ) : null}

            {redemption === 'failed' && redemptionError ? (
              <>
                <PageHead
                  eyebrow="Account"
                  title={redemptionError.title}
                  lede="Nothing has changed on your account."
                />
                <section className="panel">
                  <div className="panel__body">
                    <div
                      className="error-summary"
                      ref={announcementRef}
                      role="alert"
                      tabIndex={-1}
                    >
                      <p className="text-sm text-secondary">{redemptionError.body}</p>
                    </div>
                  </div>
                  <div className="panel__footer">
                    <p className="text-sm text-secondary">
                      <Link to="/sign-in">Sign in</Link> to send yourself a new link.
                    </p>
                  </div>
                </section>
              </>
            ) : null}

            {redemption === 'idle' ? (
              <>
                <PageHead
                  eyebrow="Account"
                  title="Confirm your email address"
                  lede="One step left before Onyx can work with your tax position."
                />
                <section className="panel">
                  <div className="panel__body">
                    <div className="stack stack-5">
                      <p className="text-sm text-secondary">
                        We sent a link to {addressed}. Open it and your account is
                        ready. Links work once and expire after a day.
                      </p>
                      <p className="text-sm text-secondary">
                        Onyx asks for this before showing you anything about your
                        money, so that a mistyped address cannot become somebody
                        else&rsquo;s tax information.
                      </p>

                      {resendState === 'sent' ? (
                        <div className="error-summary" role="status">
                          <p className="text-sm">
                            Sent. Check your inbox, including spam.
                          </p>
                        </div>
                      ) : null}

                      {resendError ? (
                        <div className="error-summary" role="alert">
                          <h2 className="text-sm mb-2">{resendError.title}</h2>
                          <p className="text-sm text-secondary">{resendError.body}</p>
                        </div>
                      ) : null}

                      <div>
                        <button
                          className="btn btn--primary"
                          disabled={resendState === 'sending'}
                          onClick={handleResend}
                          type="button"
                        >
                          {resendState === 'sending' ? 'Sending…' : 'Send it again'}
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="panel__footer">
                    <div className="stack stack-3">
                      <p className="text-sm text-secondary">
                        Wrong address? Sign out and register with the right one.
                      </p>
                      <div>
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => void signOut()}
                          type="button"
                        >
                          Sign out
                        </button>
                      </div>
                    </div>
                  </div>
                </section>
              </>
            ) : null}
          </div>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
