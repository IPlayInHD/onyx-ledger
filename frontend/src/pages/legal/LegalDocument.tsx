/* =========================================================================
   LEGAL AND POLICY CENTRE — one document
   =========================================================================
   A public route that renders one document from the content module. There is
   no fetch and no async state here on purpose: policy text is part of the
   build, so what a customer reads is fixed by the version they are looking at
   rather than by whatever a service happened to return.

   Two things this file refuses to do:

     it never renders HTML from data   — sections are plain React children, so
                                         policy copy cannot carry markup
     it never renders a blank page     — an unknown id is a real answer, given
                                         with a way back rather than a crash

   The draft banner is not optional and is not conditional. Every document in
   this centre is a draft that has not had Canadian legal review, and the
   banner sits between the title and the text so it cannot be scrolled past
   without being seen.
   ========================================================================= */
import { Fragment, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import { isoDate } from '@/lib/format'
import { LEGAL_DOCS, LEGAL_ORDER, type LegalDoc } from './content'

/** Public chrome, shared by the document and the not-found answer so both are
 *  reachable, printable and navigable in the same way. */
function PublicFrame({ children }: { children: ReactNode }) {
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="brandbar no-print">
        <div className="shell-container brandbar__inner">
          <Wordmark to="/" />
          <nav className="row row-2 wrap" aria-label="Legal centre">
            <Link className="btn btn--ghost" to="/legal">
              All policies
            </Link>
            <Link className="btn btn--ghost" to="/trust">
              Trust Centre
            </Link>
          </nav>
        </div>
      </header>

      <main className="shell__main" id="main">
        <div className="shell-container">{children}</div>
      </main>

      <SiteFooter />
    </div>
  )
}

function DraftBanner() {
  return (
    <div className="doc__draft">
      <p>
        <strong>Draft for review.</strong> This document is a draft prepared for
        review. It has not been reviewed by Canadian legal counsel, it is not
        legal advice, and it must receive professional review before it is
        relied on in production. Where a decision has not been made — a
        governing province, a retention period, a contact address — it is left
        marked as open rather than filled in with something that sounds settled.
      </p>
    </div>
  )
}

function DocumentMeta({ doc }: { doc: LegalDoc }) {
  return (
    <dl className="doc__meta">
      <div>
        <dt>Version</dt>
        <dd>{doc.version}</dd>
      </div>
      <div>
        <dt>Effective</dt>
        <dd>{isoDate(doc.effectiveDate)}</dd>
      </div>
      <div>
        <dt>Last updated</dt>
        <dd>{isoDate(doc.lastUpdated)}</dd>
      </div>
    </dl>
  )
}

/** The rest of the centre, so a reader who has finished one document does not
 *  have to go back to the index to find the next one. */
function OtherDocuments({ currentId }: { currentId: string }) {
  const others = LEGAL_ORDER.map((id) => LEGAL_DOCS[id]).filter(
    (doc): doc is LegalDoc => doc !== undefined && doc.id !== currentId,
  )
  if (others.length === 0) return null

  return (
    <nav
      className="no-print"
      aria-label="Other policy documents"
      style={{ marginTop: 'var(--space-9)', maxWidth: 'var(--measure)' }}
    >
      <div className="stack stack-3">
        <span className="eyebrow">Other policy documents</span>
        <div className="row row-4 wrap">
          {others.map((doc) => (
            <Link className="text-sm" key={doc.id} to={`/legal/${doc.id}`}>
              {doc.title}
            </Link>
          ))}
        </div>
      </div>
    </nav>
  )
}

function DocumentNotFound() {
  return (
    <div className="doc">
      <PageHead
        eyebrow="Legal and policies"
        title="That policy document was not found."
        lede="No document is published at this address. The link may be out of date, or the document may have been renamed."
      />
      <p>
        Nothing has gone wrong with your account, and no policy has been
        withdrawn without notice. The legal and policy centre lists every
        document Onyx Ledger publishes, each with its version and the date it
        last changed.
      </p>
      <p style={{ marginTop: 'var(--space-5)' }}>
        <Link className="btn btn--primary" to="/legal">
          See all policy documents
        </Link>
      </p>
    </div>
  )
}

export default function LegalDocument() {
  const { documentId } = useParams()
  const doc = documentId ? LEGAL_DOCS[documentId] : undefined

  if (!doc) {
    return (
      <PublicFrame>
        <DocumentNotFound />
      </PublicFrame>
    )
  }

  return (
    <PublicFrame>
      <article className="doc">
        <PageHead
          eyebrow="Legal and policies"
          title={doc.title}
          lede={doc.summary}
        />

        <DraftBanner />
        <DocumentMeta doc={doc} />

        {doc.sections.map((section) => (
          <Fragment key={section.heading}>
            <h2>{section.heading}</h2>
            {section.paragraphs?.map((paragraph) => (
              <p key={paragraph}>{paragraph}</p>
            ))}
            {section.bullets && section.bullets.length > 0 ? (
              <ul>
                {section.bullets.map((bullet) => (
                  <li key={bullet}>{bullet}</li>
                ))}
              </ul>
            ) : null}
          </Fragment>
        ))}
      </article>

      <OtherDocuments currentId={doc.id} />
    </PublicFrame>
  )
}
