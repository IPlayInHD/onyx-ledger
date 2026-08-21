/* =========================================================================
   ONYX TRUST CENTRE  —  public, /trust
   =========================================================================
   The page a careful person reads BEFORE handing a piece of software their
   financial facts. It is a product surface, not a policy: it describes the
   system that is actually running, in plain language, and it links to the
   policies rather than restating them.

   Two disciplines govern every sentence below.

   Only true things. Each claim here was written against the certified backend
   — the deterministic engine, the TKMS publication pipeline, the explanation
   validators, row-level tenant isolation, Argon2id password storage, the
   admission limiter, and the audit redaction trigger. Nothing is described as
   stronger than it is, and the security section closes by saying what nobody
   can promise, because a trust page that oversells is self-defeating.

   Teach the vocabulary. The real <Provenance>, <FreshnessBadge>,
   <IntegrityBadge> and <TrustPair> components are rendered here, not
   illustrations of them. A customer who learns the marks on this page reads
   every other screen correctly, and that is the whole point of having them.

   Public chrome: no AppShell. The nav rail and the tax-year picker mean
   nothing to a signed-out visitor, and this page must be readable without an
   account.
   ========================================================================= */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'
import {
  FreshnessBadge,
  IntegrityBadge,
  Provenance,
  TrustPair,
  type FreshnessStatus,
  type IntegrityState,
  type ProvenanceKind,
} from '@/components/trust'
import { useAuth } from '@/auth/AuthProvider'

/* The single source of truth for the page outline. The table of contents and
   the sections are both generated from it, so a link can never point at an id
   that no longer exists. */
const SECTIONS = [
  { id: 'calculations', title: 'How calculations work' },
  { id: 'tax-authority', title: 'How tax authority works' },
  { id: 'ai', title: 'How AI is used' },
  { id: 'assumptions', title: 'How assumptions work' },
  { id: 'history', title: 'How historical results work' },
  { id: 'data-protection', title: 'How your data is protected' },
  { id: 'policies', title: 'Where to read more' },
] as const

type SectionId = (typeof SECTIONS)[number]['id']

function titleOf(id: SectionId): string {
  const match = SECTIONS.find((section) => section.id === id)
  return match ? match.title : ''
}

/* Each section is focusable so that following a link from the contents moves
   keyboard focus into the section, not just the viewport. Without this, a
   keyboard user who "jumps" to section five continues tabbing from section
   one. */
function Section({
  id,
  children,
}: {
  id: SectionId
  children: ReactNode
}) {
  return (
    <section id={id} aria-labelledby={`${id}-heading`} tabIndex={-1}>
      <h2 id={`${id}-heading`}>{titleOf(id)}</h2>
      {children}
    </section>
  )
}

/* ------------------------------------------------------------- section 1 -- */

/** The lineage of one figure, drawn with the trust layer's own trace. Read top
 *  to bottom it answers "where did this number come from", which is the
 *  question the rest of the page elaborates on. */
const LINEAGE: { kind: ProvenanceKind; title: string; detail: string }[] = [
  {
    kind: 'user',
    title: 'The facts on your account',
    detail:
      'Income, registered accounts, expenses and the other details you entered, together with values Onyx derived from records you supplied.',
  },
  {
    kind: 'governed',
    title: 'The governed tax data for that year',
    detail:
      'Rates, brackets, limits, thresholds and the conditions that decide whether a provision applies, read from published, version-controlled sources.',
  },
  {
    kind: 'calculated',
    title: 'The deterministic engine',
    detail:
      'The rules applied as code. The same inputs produce the same result every time, and the engine version that produced it is recorded with the result.',
  },
  {
    kind: 'ai',
    title: 'An explanation, afterwards',
    detail:
      'If an explanation is shown, it is written after the figures are settled, from those figures. It cannot change any of them.',
  },
]

/* ------------------------------------------------------------- section 2 -- */

const PIPELINE: { lead: string; body: string }[] = [
  {
    lead: 'Registration.',
    body: 'A source document is registered with a fingerprint taken from its exact bytes. If one byte of the source differs, it is a different source and Onyx can tell. What was read stays recoverable later rather than becoming a matter of recollection.',
  },
  {
    lead: 'Validation.',
    body: 'Content taken from a source is checked before it can become a rule version: structure, required fields, and conflicts with what is already published. Content that fails validation does not proceed, and the validation report is kept with the version.',
  },
  {
    lead: 'Four-eyes approval.',
    body: 'A change is submitted by one authorised identity and approved by a different one. Whoever submits a change cannot be the person who approves it. That separation is enforced by the database as well as by the application, and it applies equally to rolling a rule back to an earlier version.',
  },
  {
    lead: 'Publication.',
    body: 'Only an authorised identity holding the publishing permission, acting on an approved and validated change, can publish. The version that was live is superseded rather than deleted, and who published what, and when, is recorded.',
  },
  {
    lead: 'Reading.',
    body: 'The engine reads published versions only. Drafts, submitted changes and rejected changes are invisible to calculation. Nothing reaches your figures because somebody was part-way through editing it.',
  },
]

/* ------------------------------------------------------------- section 3 -- */

const AI_CANNOT: { lead: string; body: string }[] = [
  {
    lead: 'It cannot change an amount.',
    body: 'Figures come from the engine. The model is given them and has no path to writing a different one: every number in its wording is matched against the values it was supplied.',
  },
  {
    lead: 'It cannot decide eligibility.',
    body: 'Whether a provision applies to you is a governed determination made from your recorded facts. The model reports that determination and is rejected if it states it more favourably than the engine did.',
  },
  {
    lead: 'It cannot invent a citation.',
    body: 'It may refer only to sources that were supplied to it. A reference to anything else fails the check.',
  },
  {
    lead: 'It cannot invent a deadline or an evidence requirement.',
    body: 'Dates and required documents come from the engine’s output. A deadline or a document the model produced on its own is a rejection, not a helpful extra.',
  },
]

/* ------------------------------------------------------------- section 4 -- */

/* The four kinds of value behind a figure, with the badge and the exact term
   the product uses for each. These are the real badges: what a customer learns
   here is what they will see on the position, the twin and every opportunity. */
const CERTAINTIES: {
  kind: ProvenanceKind
  term: string
  heading: string
  body: string
}[] = [
  {
    kind: 'governed',
    term: 'Published limit',
    heading: 'A published statutory limit',
    body: 'A rate, limit or threshold taken from a published tax source, with the version it came from recorded alongside it. Nobody at Onyx chose the number.',
  },
  {
    kind: 'user',
    term: 'You stated this',
    heading: 'A value you stated',
    body: 'A fact you entered. Onyx uses it exactly as you gave it and does not quietly adjust it. Because everything built on it depends on it being right, it is always marked as yours rather than absorbed into the result.',
  },
  {
    kind: 'assumption',
    term: 'Assumed by Onyx',
    heading: 'A platform assumption',
    body: 'A value Onyx assumed so that a model could run — an amount of contribution room, for instance, where you have not told us the real figure and Onyx holds no record of it. It is used for modelling and it has not been verified. Confirm the real figure before you act on anything that depends on it.',
  },
  {
    kind: 'calculated',
    term: 'Derived from your records',
    heading: 'A value derived from your records',
    body: 'A value worked out from documents and records you supplied rather than one you typed. It is only as good as the records behind it, which is why it is distinguished from a figure you stated yourself.',
  },
]

/* ------------------------------------------------------------- section 5 -- */

const FRESHNESS_STATES: { status: FreshnessStatus; meaning: string }[] = [
  {
    status: 'current',
    meaning:
      'Nothing Onyx knows about has changed since this result was produced.',
  },
  {
    status: 'stale',
    meaning:
      'Newer information exists — a fact you changed, or a source that was republished. Re-run to get a statement about today.',
  },
  {
    status: 'superseded',
    meaning:
      'A newer result has replaced this one. This is kept as the historical version rather than overwritten.',
  },
  {
    status: 'unknown',
    meaning:
      'Freshness has not been evaluated for this result, so Onyx will not claim it either way.',
  },
]

const INTEGRITY_STATES: { state: IntegrityState; meaning: string }[] = [
  {
    state: 'verified',
    meaning:
      'The sealed result was re-run from its own pinned inputs and matched.',
  },
  {
    state: 'non_reproducible',
    meaning:
      'Re-running it from its pinned inputs produced something different. Treat the figures with caution.',
  },
  {
    state: 'unavailable',
    meaning:
      'A pinned input could not be loaded, so nothing was compared. This is not a mark against the result.',
  },
  {
    state: 'legacy_unverifiable',
    meaning:
      'The result predates Onyx recording everything a replay needs. Re-run to get a verifiable one.',
  },
  {
    state: 'not_checked',
    meaning: 'No replay has been run for this result yet.',
  },
]

/* ------------------------------------------------------------- section 7 -- */

const POLICIES: { to: string; label: string; body: string }[] = [
  {
    to: '/legal/ai-transparency',
    label: 'AI transparency',
    body: 'The full statement of how Onyx uses AI, what it is not permitted to do, and what happens when generated wording fails a check.',
  },
  {
    to: '/legal/privacy',
    label: 'Privacy Policy',
    body: 'What personal information Onyx holds, why it holds it, and how it is handled.',
  },
  {
    to: '/legal/security',
    label: 'Security',
    body: 'The security position in more detail than this page carries.',
  },
  {
    to: '/legal/tax-disclaimer',
    label: 'Tax disclaimer',
    body: 'The limits of software-generated tax information, and where a qualified professional is needed instead.',
  },
]

/* --------------------------------------------------------- public header -- */

/** A signed-in customer arrives here from the account menu; a visitor arrives
 *  from the landing page. Offering "Sign in" to somebody already signed in
 *  would be the first small thing on the page that is not true. */
function HeaderNav() {
  const { status } = useAuth()

  if (status === 'restoring') return null

  if (status === 'authenticated') {
    return (
      <nav className="row row-2 wrap" aria-label="Account">
        <Link className="btn btn--secondary" to="/app">
          Back to your position
        </Link>
      </nav>
    )
  }

  return (
    <nav className="row row-2 wrap" aria-label="Account">
      <Link className="btn btn--ghost" to="/sign-in">
        Sign in
      </Link>
      <Link className="btn btn--primary" to="/sign-up">
        Create account
      </Link>
    </nav>
  )
}

/* ------------------------------------------------------------------ page -- */

export default function TrustCentre() {
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="brandbar no-print">
        <div className="shell-container brandbar__inner">
          <Wordmark to="/" />
          <HeaderNav />
        </div>
      </header>

      <main className="shell__main" id="main">
        <div className="shell-container">
          <PageHead
            eyebrow="Trust Centre"
            title="How Onyx works"
            lede="Onyx asks you for your financial facts and then tells you where you stand. This page explains how the figures are produced, where the tax rules come from, what the AI does and does not do, and what protects your account."
          />

          <div className="doc">
            <p>
              This is not a legal document. It describes the system that is
              actually running behind the product, in ordinary language, so that
              creating an account is an informed decision. The binding documents
              are linked at the end, and nothing here replaces them.
            </p>

            {/* --------------------------------------- table of contents */}
            <nav
              className="panel panel--sunken no-print mt-6"
              aria-label="On this page"
            >
              <div className="panel__body">
                <span className="eyebrow">On this page</span>
                <ul className="mt-3">
                  {SECTIONS.map((section) => (
                    <li key={section.id}>
                      <a href={`#${section.id}`}>{section.title}</a>
                    </li>
                  ))}
                </ul>
              </div>
            </nav>

            {/* ============================================ 1. calculations */}
            <Section id="calculations">
              <p>
                Every tax figure Onyx shows you is produced by a deterministic
                calculation engine. The engine takes two things — the facts
                recorded on your account, and the governed tax data published
                for that tax year — applies the rules as code, and returns a
                result. The tax rules themselves are not written into the
                application: they are data the engine reads.
              </p>
              <p>
                Deterministic means the same inputs always produce the same
                result. There is no sampling and no element of chance in a
                figure. Run the same analysis twice over unchanged facts and
                unchanged tax data and you get the same numbers, with the
                version of the engine that produced them recorded alongside.
              </p>
              <p>
                Generative AI does not calculate tax. No language model is
                consulted while a figure is produced, and no model output is
                ever used as a number. What AI does do is the subject of{' '}
                <a href="#ai">{titleOf('ai')}</a>.
              </p>
              <p>
                Amounts travel from the engine to your screen as exact decimal
                values. The screens you read format what the engine returned;
                they do not re-do the arithmetic, and they do not round a figure
                into a friendlier shape.
              </p>

              <h3>Where a figure comes from</h3>
              <div className="trace">
                {LINEAGE.map((node) => (
                  <div
                    className={`trace__node trace__node--${node.kind}`}
                    key={node.kind}
                  >
                    <div className="trace__title">{node.title}</div>
                    <p className="trace__detail">{node.detail}</p>
                  </div>
                ))}
              </div>
            </Section>

            {/* =========================================== 2. tax authority */}
            <Section id="tax-authority">
              <p>
                Rates, brackets, limits, thresholds and the conditions that
                decide whether a provision applies are held as governed data.
                They reach the engine through a controlled pipeline, and every
                stage of it exists to make one thing true: what the engine used
                is knowable afterwards.
              </p>
              <ul>
                {PIPELINE.map((stage) => (
                  <li key={stage.lead}>
                    <strong>{stage.lead}</strong> {stage.body}
                  </li>
                ))}
              </ul>
              <p>
                Each result records the published versions it read. When a
                source is later republished, results built on the earlier
                version are marked rather than quietly rewritten — see{' '}
                <a href="#history">{titleOf('history')}</a>.
              </p>
            </Section>

            {/* ===================================================== 3. AI */}
            <Section id="ai">
              <p>
                A language model writes explanations. That is the whole of its
                role. It is handed figures Onyx has already calculated, in a
                structured form, and asked to put them into sentences you can
                read.
              </p>

              <h3>What it is not able to do</h3>
              <ul>
                {AI_CANNOT.map((item) => (
                  <li key={item.lead}>
                    <strong>{item.lead}</strong> {item.body}
                  </li>
                ))}
              </ul>

              <h3>What happens before you see it</h3>
              <p>
                Generated wording is checked automatically against the
                structured figures it was given. Amounts must match exactly — a
                number close to the right one is a failure, not a rounding.
                Citations must be among those supplied. An assumption may not be
                described as more certain than it is. Freshness and integrity
                may not be misstated. A support signal may not be presented as a
                probability that a claim will be accepted.
              </p>
              <p>
                If any check fails, the explanation is discarded whole. It is
                never edited into shape and shown anyway. Onyx then shows a
                deterministic explanation written from the same figures without
                a model, and you are not left without an answer.
              </p>

              <h3>The product works with no AI at all</h3>
              <p>
                If no model is configured, or the model errors, times out or
                returns something unusable, every screen still explains itself.
                The deterministic explanation is a complete answer rather than a
                degraded one. AI improves the wording; the product does not
                depend on it.
              </p>
              <p>
                Wherever model wording appears it carries its own mark —{' '}
                <Provenance kind="ai" /> — so it can never be mistaken for a
                calculated field. Model output is treated as untrusted text from
                end to end: it is displayed as text, and it is never given any
                authority over what the product does.
              </p>
              <p>
                <Link to="/legal/ai-transparency">
                  Read the full AI transparency statement
                </Link>
              </p>
            </Section>

            {/* ============================================ 4. assumptions */}
            <Section id="assumptions">
              <p>
                Sometimes a calculation needs a value nobody has told Onyx.
                Rather than refuse to model anything, Onyx will assume one — and
                then say so, everywhere that value has an effect.
              </p>
              <p>
                There are four kinds of value behind a figure. Each is marked
                with the same badge on every screen, because the difference
                between them is what decides whether a number is safe to act on.
              </p>

              <div
                className="stack stack-5 mt-5"
              >
                {CERTAINTIES.map((certainty) => (
                  <div className="stack stack-2" key={certainty.kind}>
                    <span>
                      <Provenance kind={certainty.kind} label={certainty.term} />
                    </span>
                    <h3 className="mt-1">
                      {certainty.heading}
                    </h3>
                    <p>{certainty.body}</p>
                  </div>
                ))}
              </div>

              <p className="mt-5">
                The wording beside each badge is fixed by the kind. A value Onyx
                assumed is never described as known, confirmed, or something you
                provided. Where a result leans on an assumption, Onyx also
                limits how well supported it will claim that result is, and says
                that the limit was applied rather than hiding it.
              </p>
              <p>
                If an assumed value matters to a decision you are about to make,
                confirm the real figure — with the Canada Revenue Agency, or on
                your most recent notice of assessment — before you act.
              </p>
            </Section>

            {/* ================================================ 5. history */}
            <Section id="history">
              <p>
                When Onyx produces a result it seals it. The inputs that went
                in, the versions of the governed tax data, and the version of
                the engine are recorded with the result, and a sealed result is
                not edited afterwards. A later run produces a new result; it
                does not rewrite the old one.
              </p>
              <p>
                Because a result is sealed with what produced it, it can be
                re-run from those same pinned inputs to check that it still
                produces the same thing. That is a reproducibility check, and it
                is a different question from whether the result is still true
                today. Onyx keeps the two apart and always shows them
                separately.
              </p>

              <h3>Is this still true today?</h3>
              <p>
                Freshness. Newer information may exist — a fact you changed, or
                a source that was republished. A result that is out of date is
                still a correct record of what was calculated at the time.
              </p>
              <div className="stack stack-3">
                {FRESHNESS_STATES.map((item) => (
                  <div className="row row-3 wrap" key={item.status}>
                    <FreshnessBadge status={item.status} />
                    <span className="text-sm text-secondary grow">
                      {item.meaning}
                    </span>
                  </div>
                ))}
              </div>

              <h3>Can this be reproduced?</h3>
              <p>
                Integrity. Re-running the sealed result from its pinned inputs
                either reproduces it or does not. If a pinned input cannot be
                loaded then nothing was compared, and the honest answer is that
                it could not be checked — which is not a mark against the
                result.
              </p>
              <div className="stack stack-3">
                {INTEGRITY_STATES.map((item) => (
                  <div className="row row-3 wrap" key={item.state}>
                    <IntegrityBadge state={item.state} />
                    <span className="text-sm text-secondary grow">
                      {item.meaning}
                    </span>
                  </div>
                ))}
              </div>

              <h3>Why they are never merged</h3>
              <p>
                A result can be perfectly reproducible and completely out of
                date, or current and impossible to verify. A single green tick
                covering both would tell you something nobody checked, so Onyx
                shows the pair, labelled, wherever a sealed result appears.
              </p>

              <div
                className="panel panel--sunken mt-4"
              >
                <div className="panel__body stack stack-3">
                  <span className="eyebrow">
                    A result that reproduces exactly, but predates a change you
                    made
                  </span>
                  <TrustPair freshness="stale" integrity="verified" />
                </div>
              </div>

              <p className="mt-5">
                Neither answer is a statement that the tax treatment is correct.
                Reproducing a calculation confirms the calculation — not the law
                as it applies to your circumstances.
              </p>
            </Section>

            {/* ======================================== 6. data protection */}
            <Section id="data-protection">
              <p>
                What follows describes controls that are in place today. It
                deliberately avoids claims Onyx cannot stand behind, so if
                something is not stated here, please do not assume it.
              </p>

              <h3>Your records are isolated at the database level</h3>
              <p>
                Every record belonging to a customer carries the account it
                belongs to, and the database enforces that ownership with
                row-level policies. Each request runs inside a session scoped to
                exactly one account, so one account’s records cannot be read
                through another account’s session — even if the application
                asked for them by mistake. Ownership is checked in the
                application layer as well, so the isolation does not rest on a
                single point.
              </p>

              <h3>Passwords are never stored in readable form</h3>
              <p>
                Passwords are stored using Argon2id, a modern password-hashing
                algorithm designed to be slow and memory-hungry so that guessing
                at scale is expensive. Onyx keeps the hash and not the password:
                it cannot show you your password and cannot recover it, which is
                why a reset replaces it rather than retrieving it. Session,
                reset and verification tokens are likewise stored only as
                hashes, and the usable value exists only in transit to your
                browser.
              </p>

              <h3>Expensive operations are rate- and concurrency-limited</h3>
              <p>
                Sign-in attempts, analyses, scenario runs, optimisation runs and
                document processing are bounded both in how often they may be
                requested and in how many may run at once. That keeps the
                service responsive for everyone, and it limits how quickly
                anyone can try things against an account.
              </p>

              <h3>Audit records are scrubbed of credentials</h3>
              <p>
                Onyx keeps an append-only record of changes to your data, which
                is what lets the product tell you what changed and when. Named
                credential fields — password hashes, token hashes — have their
                values replaced with a marker before an audit row is written, so
                a credential does not enter that record.
              </p>

              <h3>What Onyx will not claim</h3>
              <p>
                No system can promise perfect security, and Onyx does not. This
                page describes what is in place now; hardening for production
                continues, and controls will be added and strengthened over
                time. Onyx makes no certification claim on this page, and it
                will not tell you that your data cannot be breached.
              </p>
              <p>
                <Link to="/legal/security">Read the Security policy</Link>
              </p>
            </Section>

            {/* =============================================== 7. policies */}
            <Section id="policies">
              <p>
                These documents govern. Where this page summarises for
                readability, they are the ones that bind.
              </p>
              <ul>
                {POLICIES.map((policy) => (
                  <li key={policy.to}>
                    <Link to={policy.to}>{policy.label}</Link> — {policy.body}
                  </li>
                ))}
              </ul>
              <p>
                Onyx Ledger produces software-generated tax information and
                estimates. It does not file anything on your behalf, and it is
                not affiliated with or endorsed by the Canada Revenue Agency.
              </p>
            </Section>
          </div>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
