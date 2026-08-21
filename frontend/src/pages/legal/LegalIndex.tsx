/* =========================================================================
   LEGAL AND POLICY CENTRE — index
   =========================================================================
   A public route, reachable without an account and without JavaScript-gated
   navigation, because a product that asks people for their income before it
   will show them anything has the order of that exchange backwards. Everything
   Onyx Ledger commits to in writing is readable first.

   Public chrome only: AppShell carries the tax-year context and the product
   navigation, neither of which means anything to a signed-out visitor.

   The index states the draft position once, at the top, so a visitor learns
   that these documents have not had Canadian legal review BEFORE they choose
   which one to open — not only after.
   ========================================================================= */
import { Link } from 'react-router-dom'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { isoDate } from '@/lib/format'
import { LEGAL_DOCS, LEGAL_ORDER, type LegalDoc } from './content'

/** The published set, in the reading order the content module declares. An id
 *  in the order with no document behind it is skipped rather than rendered as
 *  a dead link. */
function publishedDocuments(): LegalDoc[] {
  return LEGAL_ORDER.map((id) => LEGAL_DOCS[id]).filter(
    (doc): doc is LegalDoc => doc !== undefined,
  )
}

function DocumentEntry({ doc }: { doc: LegalDoc }) {
  return (
    <div className="feature">
      <h3 className="feature__title">
        <Link to={`/legal/${doc.id}`}>{doc.title}</Link>
      </h3>
      <p className="feature__body">{doc.summary}</p>
      <p className="text-xs text-muted mt-3">
        Version {doc.version} · Last updated {isoDate(doc.lastUpdated)}
      </p>
    </div>
  )
}

export default function LegalIndex() {
  const documents = publishedDocuments()

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="brandbar no-print">
        <div className="shell-container brandbar__inner">
          <Wordmark to="/" />
          <nav className="row row-2 wrap" aria-label="Site">
            <Link className="btn btn--ghost" to="/trust">
              Trust Centre
            </Link>
            <Link className="btn btn--ghost" to="/sign-in">
              Sign in
            </Link>
          </nav>
        </div>
      </header>

      <main className="shell__main" id="main">
        <div className="shell-container">
          <PageHead
            eyebrow="Legal and policies"
            title="Legal and policy centre"
            lede="Everything Onyx Ledger commits to in writing, in one place. Each document states its version, when it took effect and when it last changed."
          />

          <div className="doc">
            <div className="doc__draft">
              <p>
                <strong>These documents are drafts.</strong> All six were
                prepared for review. None of them has been reviewed by Canadian
                legal counsel, none of them is legal advice, and each must
                receive professional review before it is relied on in
                production. They are published in this state because publishing
                a polished document that nobody qualified has read would be the
                less honest option.
              </p>
            </div>
          </div>

          <section aria-labelledby="documents-heading">
            <div
              className="stack stack-2 mb-6"
            >
              <span className="eyebrow">Published documents</span>
              <h2 className="section-title" id="documents-heading">
                What Onyx Ledger states in writing
              </h2>
            </div>

            <div className="feature-grid feature-grid--3">
              {documents.map((doc) => (
                <DocumentEntry doc={doc} key={doc.id} />
              ))}
            </div>
          </section>

          <section
            className="panel panel--sunken mt-9"
            aria-labelledby="versioning-heading"
          >
            <div className="panel__header">
              <h2 className="section-title" id="versioning-heading">
                How to read the version line
              </h2>
            </div>
            <div className="panel__body">
              <div className="doc">
                <p>
                  Every document carries three dates for three different
                  questions. <strong>Version</strong> identifies the exact text
                  you are reading. <strong>Effective</strong> is the date that
                  text applies from. <strong>Last updated</strong> is when it
                  last changed. A change to any of these documents changes the
                  version, so a policy cannot be edited quietly underneath a
                  reader.
                </p>
                <p>
                  All six currently sit at the same version because they were
                  drafted together and will be reviewed together. Letting them
                  drift apart would imply a review history that has not
                  happened.
                </p>
                <p>
                  The Trust Centre covers a different question: not what Onyx
                  Ledger promises, but how it decides what to show you and where
                  each figure came from. <Link to="/trust">Read the Trust Centre</Link>.
                </p>
              </div>
            </div>
          </section>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
