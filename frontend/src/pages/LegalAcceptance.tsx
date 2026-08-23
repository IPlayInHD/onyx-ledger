/* =========================================================================
   REVIEW AND ACCEPT  —  the gate a signed-in customer clears
   =========================================================================
   Reached when the backend says agreement to the current documents is
   outstanding: at first sign-in, and again whenever counsel publishes a
   version marked as requiring re-acceptance.

   WHAT THIS SCREEN REFUSES TO DO, and each refusal is a decision:

     · NO PRE-TICKED BOX. A box the product ticked is the product asserting
       agreement on the customer's behalf, which is the exact thing the
       durable record is supposed to prove did not happen.
     · NO WALL OF TEXT IN A MODAL. The documents are long and they are real
       pages with headings, a contents order and a draft banner. This screen
       links to them and opens them in a new tab, so reading one does not
       destroy the state of accepting the other.
     · NO MODAL AT ALL, therefore no focus trap. It is a route. A customer can
       tab out of it, use browser back, and reach the footer — B4 §32, and the
       reason it is a page rather than a dialog.
     · NOTHING IS CLAIMED BEFORE IT IS STORED. Each row flips to accepted only
       after its own request returns, and a failure leaves that row exactly as
       it was with the error beside it.

   THE VERSIONS COME FROM THE SERVER, every time. This file contains no
   version constant and no document list. A bundle that shipped its own would
   be a second registry, and the failure it produces is a durable record
   saying somebody accepted 2.0 while their browser rendered 1.9.
   ========================================================================= */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { legalApi, type LegalDocumentState } from '@/api/endpoints'
import { useAuth } from '@/auth/AuthProvider'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { describeError, LoadingBlock } from '@/components/states'

/** Titles for the slugs the registry publishes.
 *
 *  A LABEL MAP, NOT A REGISTRY. It carries no version, no effective date and
 *  no opinion about what must be accepted — all three come from the server. A
 *  slug with no entry here still renders, using the slug itself, so a document
 *  added on the backend appears immediately rather than vanishing because the
 *  frontend had not been taught its name. */
const TITLES: Record<string, string> = {
  terms: 'Terms of Service',
  privacy: 'Privacy Policy',
  'ai-transparency': 'AI Transparency Statement',
  'tax-disclaimer': 'Tax Information Disclaimer',
  accessibility: 'Accessibility',
  security: 'Security',
}

function titleFor(documentType: string): string {
  return TITLES[documentType] ?? documentType
}

function describeAcceptFailure(error: unknown): { title: string; body: string } {
  if (error instanceof ApiError && error.isLegalVersionStale) {
    return {
      title: 'This document changed while you were reading',
      body: 'A newer version was published. Reload to see it — nothing has been recorded against the old one.',
    }
  }
  return describeError(error)
}

export default function LegalAcceptance() {
  const { recheck, signOut } = useAuth()
  const navigate = useNavigate()

  const [documents, setDocuments] = useState<LegalDocumentState[] | null>(null)
  const [loadError, setLoadError] = useState<{ title: string; body: string } | null>(null)

  /* Per-document, so one failure never blocks the other and a slow request
     never disables a button the customer has not pressed. */
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({})
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [errors, setErrors] = useState<Record<string, { title: string; body: string }>>({})

  const announcement = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    setLoadError(null)
    try {
      const state = await legalApi.state()
      setDocuments(state.documents)
      return state
    } catch (error) {
      setLoadError(describeError(error))
      return null
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const outstanding = (documents ?? []).filter((d) => d.acceptance_outstanding)

  /* Everything required is accepted: re-read the session so the app opens.
     `recheck` is what turns `legal-outstanding` back into `authenticated`. */
  useEffect(() => {
    if (documents === null || outstanding.length > 0) return
    void (async () => {
      try {
        await recheck()
        navigate('/app', { replace: true })
      } catch {
        /* Leaving them here with everything accepted is a dead end, so fall
           back to a reload of the state rather than a silent stall. */
        void load()
      }
    })()
  }, [documents, outstanding.length, recheck, navigate, load])

  async function handleAccept(document: LegalDocumentState) {
    const key = document.document_type
    setErrors((previous) => ({ ...previous, [key]: undefined as never }))
    setSaving((previous) => ({ ...previous, [key]: true }))
    try {
      // The version the SERVER said is current, taken from the state response
      // rather than from anything this bundle knows.
      await legalApi.accept(key, document.current_version)
      // Re-read rather than patching local state: the row is only accepted
      // because the server says so, which is the whole contract of this
      // subsystem — nothing claims an acceptance that was not stored.
      await load()
      announcement.current?.focus()
    } catch (error) {
      setErrors((previous) => ({ ...previous, [key]: describeAcceptFailure(error) }))
      if (error instanceof ApiError && error.isLegalVersionStale) await load()
    } finally {
      setSaving((previous) => ({ ...previous, [key]: false }))
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

            <PageHead
              eyebrow="Account"
              title={
                documents && documents.some((d) => d.accepted_version !== null)
                  ? 'Our terms have changed'
                  : 'Before you start'
              }
              lede={
                documents && documents.some((d) => d.accepted_version !== null)
                  ? 'Review what changed and accept the current version to carry on.'
                  : 'Onyx needs your agreement to two documents before it works with your tax position.'
              }
            />

            {documents === null && !loadError ? (
              <LoadingBlock label="Loading the current documents" />
            ) : null}

            {loadError ? (
              <section className="panel">
                <div className="panel__body">
                  <div className="error-summary" role="alert">
                    <h2 className="text-sm mb-2">{loadError.title}</h2>
                    <p className="text-sm text-secondary">{loadError.body}</p>
                  </div>
                  <div className="mt-4">
                    <button className="btn btn--secondary" onClick={() => void load()} type="button">
                      Try again
                    </button>
                  </div>
                </div>
              </section>
            ) : null}

            <div aria-live="polite" ref={announcement} tabIndex={-1} />

            {outstanding.map((document) => {
              const key = document.document_type
              const isReacceptance = document.accepted_version !== null
              return (
                <section className="panel mb-4" key={key}>
                  <div className="panel__body">
                    <div className="stack stack-4">
                      <div>
                        <h2 className="text-base">{titleFor(key)}</h2>
                        <p className="text-xs text-muted">
                          Version {document.current_version} · in effect from{' '}
                          {document.effective_date}
                          {isReacceptance
                            ? ` · you accepted ${document.accepted_version}`
                            : null}
                        </p>
                      </div>

                      <p className="text-sm text-secondary">
                        {isReacceptance
                          ? 'This document has been updated since you accepted it. Read the current version before agreeing to it.'
                          : 'Read this document before agreeing to it.'}
                      </p>

                      {/* A NEW TAB, deliberately: reading one document must not
                          discard the state of accepting the other. */}
                      <p className="text-sm">
                        <Link
                          rel="noopener noreferrer"
                          target="_blank"
                          to={`/legal/${key}`}
                        >
                          Read the {titleFor(key)} (opens in a new tab)
                        </Link>
                      </p>

                      {errors[key] ? (
                        <div className="error-summary" role="alert">
                          <h3 className="text-sm mb-2">{errors[key].title}</h3>
                          <p className="text-sm text-secondary">{errors[key].body}</p>
                        </div>
                      ) : null}

                      <div className="field">
                        <label className="field__check" htmlFor={`confirm-${key}`}>
                          {/* NEVER pre-ticked. `checked` is driven by state
                              that starts false and only the customer changes. */}
                          <input
                            checked={confirmed[key] ?? false}
                            id={`confirm-${key}`}
                            onChange={(event) =>
                              setConfirmed((previous) => ({
                                ...previous,
                                [key]: event.target.checked,
                              }))
                            }
                            type="checkbox"
                          />
                          <span>
                            I have read and agree to the {titleFor(key)}, version{' '}
                            {document.current_version}.
                          </span>
                        </label>
                      </div>

                      <div>
                        <button
                          className="btn btn--primary"
                          disabled={!confirmed[key] || saving[key]}
                          onClick={() => void handleAccept(document)}
                          type="button"
                        >
                          {saving[key] ? 'Recording…' : `Accept ${titleFor(key)}`}
                        </button>
                      </div>
                    </div>
                  </div>
                </section>
              )
            })}

            {documents !== null && outstanding.length === 0 && !loadError ? (
              <section className="panel">
                <div className="panel__body" role="status">
                  <p className="text-sm text-secondary">
                    Everything is accepted. Taking you to Onyx…
                  </p>
                </div>
              </section>
            ) : null}

            {documents !== null ? (
              <section className="panel">
                <div className="panel__footer">
                  <div className="stack stack-3">
                    <p className="text-sm text-secondary">
                      Not ready to agree? You can sign out and come back. Your
                      account and anything already saved are untouched.
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
            ) : null}
          </div>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
