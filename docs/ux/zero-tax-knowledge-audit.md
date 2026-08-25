# Onyx Ledger — zero-tax-knowledge consumer UX audit and redesign specification

**Type:** audit and specification. **Nothing here was implemented.**
No application behaviour, component, service, schema, migration, rule,
infrastructure, API or test was changed by the entry that produced this
document. The only repository modification is this file.

**Evidence base.** Every claim below is traceable to a file, route, test or a
screen captured from the running product. The application was built and driven
in a real browser as the first-time-filer persona (`frontend/scripts/e2e.sh`
against a live API and a real Chromium), so the screens described are the
screens that render, not screens inferred from source.

---

## A. Executive verdict

**No. The current consumer experience cannot serve a zero-tax-knowledge
mass-market user — and the most important reason is not a UX problem.**

Three findings, in order of severity.

### A1. Onyx Ledger is not a return-preparation product, and the brief assumes it is

There is no return, no filing, no refund and no submission anywhere in the
system. Searching the frontend and the API for `netfile`, `efile`, `T1`, or any
notion of submitting a return returns nothing. The API surface
(`frontend/src/api/endpoints.ts`) is: auth, recovery, legal, profile,
financials, analysis, IOE, AI explanation, account, config. There is no filing
contract to put a UI on.

What Onyx actually is, and is rather good at, is a **tax position and
opportunity assurance product**: it tells a taxpayer where they stand, what it
can and cannot stand behind, what it has found that might help them, and what
would change if they acted. The landing page says so in its own words — *"Know
where you stand before tax time."* (`frontend/src/pages/Landing.tsx`).

The brief's §15 sketch — *"Your 2025 return / Estimated refund / XX% complete /
What you need to do next"* — describes a product that does not exist here. A
progress bar over a return cannot be built, because there is no return to be a
percentage of.

**This is the decision the roadmap turns on, and it is a product decision, not
a design one:**

- **If Onyx is to become a filing product**, the redesign in this brief is
  premature. The missing pieces are a return model, a line-item mapping, a
  completeness model and a filing pathway — none of which are UX work, and all
  of which change what every screen means.
- **If Onyx is to remain an assurance and optimization product**, the brief's
  north star still applies in full, but the home screen answers *"where do I
  stand and what should I do next"* rather than *"how much of my return is
  done"*, and the first-time-filer persona is not the primary persona — a
  person with no tax knowledge and one T4 has almost nothing to optimize.

Recommending one over the other is outside an audit's authority. **Section W
therefore scopes a first implementation entry that is valuable under either
answer**, and Section V marks every later phase with the branch it depends on.

### A2. The product has no consumer-language layer at all

54 call sites across the consumer pages render backend codes to users through
`humanize()` (`frontend/src/lib/format.ts`), which is not a translation
function — it lowercases the string and replaces underscores:

```ts
export function humanize(code: string | null | undefined): string {
  if (!code) return ''
  const words = code.replace(/[_.]/g, ' ').toLowerCase().trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}
```

So `INCREASE_RRSP_DEDUCTION` reaches the customer as *"Increase rrsp
deduction"*, `CONTRIBUTION_ROOM_AVAILABLE` as *"Contribution room available"*,
and `RULES_EVALUATION_FAILED` as *"Rules evaluation failed"*. The de-underscored
string is doing the job a lexicon should be doing.

This is the highest-value fixable problem in the product, and it is
**tractable**, because every vocabulary that reaches `humanize()` is a closed
enum (§N).

### A3. The first screen a new account sees is an inventory of the domain model

Captured from the running product, the signed-in empty state at `/app` shows an
"Assurance ledger" listing **ten internal families, nine of them with zero
items**, each with a raw reason code:

| Family | State | Reason shown to the customer |
|---|---|---|
| Fact | Ready | Live records authoritative |
| Tax state | Not established yet | No analysis run for tax year |
| Opportunity | Not established yet | No optimization run for tax year |
| Deadline | Not established yet | No optimization run for tax year |
| Evidence | Not established yet | No optimization run for tax year |
| Resource | Not established yet | No optimization run for tax year |
| Assumption | Not established yet | No optimization run for tax year |
| Scenario | Ready | Live records authoritative |
| Obligation | Does not apply | Reserved no producer |
| Decision | Does not apply | Reserved no producer |

*"Reserved no producer"* is an internal reason code describing Onyx's own
architecture. It is shown, verbatim, to a person who has just created an
account. The only visible header action is **"Re-run analysis"** — an offer to
re-run something that has never run once.

**What is genuinely good, and must not be lost.** The empty states are honest
and well written (*"A family shown as Not established yet means no governing
run has produced it — which is different from it being empty. Onyx does not
report an unanswered question as a clean result."*). The Trust Centre is
excellent. The explanation layer's authority boundary is already correct. The
accessibility posture is real, not decorative. None of that needs to be traded
away to make the product simpler; the simplification is almost entirely a
naming, ordering and disclosure problem sitting on top of a sound engine.

---

## B. Current journey

Mapped by driving the product. Stage names are the brief's; the "exists"
column is what is actually there.

| # | Stage | Exists | Route / file |
|---|---|---|---|
| 1 | Entry / landing | Yes | `/` `pages/Landing.tsx` |
| 2 | Signup | Yes | `/sign-up` `pages/SignUp.tsx` |
| 3 | Authentication | Yes | `/sign-in`, `/verify-email`, `/forgot-password`, `/reset-password` |
| 3b | Legal acceptance | Yes | `/legal/accept` `pages/LegalAcceptance.tsx` |
| 4 | Initial onboarding | Partial | `/app/onboarding` — a 5-step data-entry form, not an onboarding |
| 5 | Taxpayer / profile setup | Yes, twice | Onboarding step 1 **and** `/app/settings` |
| 6 | Tax-year / jurisdiction setup | Yes | year picker in `components/Shell.tsx`; province in onboarding |
| 7 | Income discovery | **No** | income is typed by hand; nothing is discovered |
| 8 | Document collection | **No UI** | backend exists (§I); no file input anywhere in `frontend/src` |
| 9 | Document review | **No UI** | `POST /documents/{id}/confirm` exists and is unreachable |
| 10 | Questions / interview | **No** | there is no interview; there is a form |
| 11 | Opportunities | Yes | `/app/opportunities` |
| 12 | Recommendations | Yes | via analysis + IOE |
| 13 | Scenarios / optimization | Yes | `/app/twin`, `/app/before-you-act/:id` |
| 14 | Missing information | Yes | `/app/evidence` |
| 15 | Review | Partial | onboarding step 5 "Review and run" |
| 16 | Result | Yes | `/app`, `/app/position` |
| 17 | Explanation | Yes | 5 "Explain this…" buttons, one per surface |
| 18 | Return / comeback | Yes | `/app/changes` "What changed" |

**Stage-by-stage, for the persona.**

**1. Landing** — `H1: "Know where you stand before tax time."` Honest, calm, no
urgency tricks. For a first-time filer it does not answer "will this do my
taxes?", because the product does not. *Value received: none yet — correctly.*

**2–3. Signup, verification, legal** — three sequential gates before the
product is reachable: create account → redeem emailed verification link →
accept every outstanding legal document. Each is individually justified
(B3 and B4 built them deliberately) and collectively they are the industry
norm. *P3.*

**4. First signed-in screen** — the empty `/app` described in A3. The one
correct element is the empty state inside "Estimated position": *"No analysis
yet for this year / Add your income and other facts, and Onyx will work out
where you stand"* with a primary **"Add your details"** button. That is the
right next action — and it is the **third** panel down, competing with
"Re-run analysis" in the header. *P0 — this is the abandonment screen.*

**5. Onboarding** — `H1: "Tell Onyx about your year"`, five steps: Province and
situation → Income → Registered accounts → Donations and medical → Review and
run. Detail in §E. *P0/P1 throughout.*

**6. Analysis** — the user presses "Run", and the position appears. **This is
the first moment of personalised value, and it is the last screen of a
five-step form.** *See §F.*

**7–13. Opportunities, Decision Twin, Evidence, What changed** — sophisticated,
well-built surfaces whose names and contents assume the reader already holds
Onyx's mental model.

---

## C. Top 20 friction points

| # | Sev | Finding | Where |
|---|---|---|---|
| 1 | **P0** | No return, no filing, no refund — the product cannot complete the job a mass-market filer arrives to do | whole API surface |
| 2 | **P0** | First signed-in screen leads with an inventory of 10 internal families, 9 empty, with raw reason codes | `pages/Overview.tsx:52,84,107` |
| 3 | **P0** | ~~The document pipeline exists in the backend and is unreachable from the UI~~ **CORRECTED — the pipeline is not complete: nothing converts an uploaded document into text, so the "Onyx reads it" step does not exist.** See `document-first-blocker.md` | `backend/app/services/document_processing/ocr.py`; no OCR dependency; `workers/tasks/documents.py` absent |
| 4 | **P0** | Onboarding asks for **"Contribution room available"** — a number a first-time filer cannot know (it is on a Notice of Assessment they have never received) | `pages/Onboarding.tsx` |
| 5 | **P0** | Income must be classified by the user into 8 tax categories, including *Eligible dividends* vs *Non-eligible dividends* | `pages/Onboarding.tsx:78` |
| 6 | **P0** | No "I don't know" / "I'm not sure" option exists anywhere in the product | all of `pages/Onboarding.tsx` |
| 7 | **P1** | Primary header action on the home screen is "Re-run analysis" — a system operation, offered before any analysis has run | `pages/Overview.tsx` |
| 8 | **P1** | Navigation is the domain model: Overview / Tax position / Opportunities / **Decision Twin** / **Evidence** / What changed | `components/Shell.tsx:70` |
| 9 | **P1** | 54 `humanize()` sites render backend enums to consumers | `pages/*.tsx` |
| 10 | **P1** | Attention item titles are opportunity codes — *"Increase rrsp deduction"* | `pages/Overview.tsx:144` |
| 11 | **P1** | Attention subtitles are action codes — *"Evidence required"* | `pages/Overview.tsx:147` |
| 12 | **P1** | Time to first value is behind an entire 5-step form (§F) | `pages/Onboarding.tsx` |
| 13 | **P1** | Province and marital status are asked in onboarding **and** in Settings | `Onboarding.tsx` + `Settings.tsx` |
| 14 | **P1** | Explanations are opt-in, per-subject, collapsed — a beginner never learns they exist | 5 × "Explain this…" |
| 15 | **P2** | Helper text is written about Onyx's integrity, not the user's task: *"A province Onyx cannot price is not offered, rather than accepted and then answered with a figure computed under the wrong rules."* | `pages/Onboarding.tsx` |
| 16 | **P2** | *"The governed rules decide what, if anything, it opens up"* — internal vocabulary in a field hint | `pages/Onboarding.tsx` |
| 17 | **P2** | Situation checkboxes assume concepts: *"investments outside a registered account"*, *"first-time home buyer"* | `pages/Onboarding.tsx:110` |
| 18 | **P2** | Six top-level destinations with no ranking — nothing says which to visit first | `components/Shell.tsx:70` |
| 19 | **P2** | Tax-year selector is global chrome; a first-time filer has exactly one year and never needs it | `components/Shell.tsx` |
| 20 | **P3** | "Assurance ledger" and "Decision Twin" are strong internal names with no consumer meaning | `Overview.tsx`, `Shell.tsx` |

---

## D. Tax-jargon inventory

`humanize()` is the mechanism; these are the largest instances. **Severity** is
consumer impact, not correctness — every technical term below is *correct*, and
several are legally load-bearing and must remain reachable.

| Current term | Where | Why it confuses | Technically necessary? | Proposed default | Technical disclosure |
|---|---|---|---|---|---|
| `Reserved no producer` | Overview ledger | Describes Onyx's architecture, not the user's tax | No | *(do not show)* | — |
| `Live records authoritative` | Overview ledger | "Authoritative" is an internal provenance term | No | "Up to date" | "Derived from live records" |
| `Not established yet` | Overview ledger | Established by whom? | No | "Nothing here yet" | "No governing run has produced this family" |
| `No optimization run for tax year` | Overview ledger | "Optimization run" is a system concept | No | "We haven't looked at this yet" | keep as detail |
| Assurance family names (Fact, Tax state, Opportunity, Deadline, Evidence, Resource, Assumption, Scenario, Obligation, Decision) | Overview ledger | Ten domain nouns, unexplained | No | collapse to one line: "What Onyx can stand behind" | full table at Level 4 |
| `Increase rrsp deduction` | Attention list | A code, de-underscored | No | "Putting money in an RRSP could lower your tax" | rule code + version |
| `Contribution room available` | Attention list | Assumes "contribution room" | Partly | "You have room to contribute" | formal term at Level 2 |
| `Evidence required` | Attention action | "Evidence" is legal/internal register | No | "We need a document" | `ACTION: EVIDENCE_REQUIRED` |
| `Decision required` | Attention action | Decision about what? | No | "We need you to choose" | `ACTION: DECISION_REQUIRED` |
| `Eligibility indeterminate` | Review reason | Two abstract words | No | "We can't confirm this yet" | `ELIGIBILITY_INDETERMINATE` |
| `Inputs changed since evaluation` | Review reason | System vocabulary | No | "Something changed — worth another look" | keep |
| `Eligible dividends` / `Non-eligible dividends` | Income picker | A distinction most filers cannot make | **Yes** | don't ask — read the slip | keep the formal term on the confirmed row |
| `Contribution room available` (field) | Onboarding | Cannot be answered from memory | **Yes** | don't ask — offer "I don't know" and treat as unknown | keep |
| `Registered accounts` | Onboarding step | Umbrella term | Partly | "Savings you've put money into (RRSP, FHSA)" | keep |
| `Marital status` | Onboarding + Settings | Fine, but asked twice | Yes | ask once | — |
| `Province you file in` | Onboarding + Settings | "File" presumes filing | Yes | "Where did you live on 31 December?" | keep |
| `Decision Twin` | Primary nav | Product name with no external meaning | No | "What if" | keep as a subtitle |
| `Evidence` | Primary nav | Legal register | No | "Documents" | keep |
| `Tax position` | Primary nav | Assumes the concept | Partly | "Your numbers" | keep |
| `Assurance ledger` | Overview | Two abstract nouns | No | "What we're sure about" | keep |

**The rule this table encodes:** never delete a technical term, and never lead
with one. Every row keeps the precise term reachable at a deeper level (§M).

---

## E. Question-burden analysis

The consumer product asks for **11 distinct labelled inputs in onboarding** and
**~46 labelled inputs across all pages** (`Opportunities.tsx` 11,
`Onboarding.tsx` 11, `DecisionTwin.tsx` 6, `Settings.tsx` 4, others 1–3).

Onboarding, classified against the brief's A–E scheme:

| Question | Class | Reasoning |
|---|---|---|
| Province you file in | **A. Required** | Drives which provincial rules apply. Cannot be inferred safely. |
| Marital status | **A. Required** | Drives credits and transfers. |
| Situation: self-employed | **C. Conditional** | Only needed if income of that type exists — which the income step already reveals. |
| Situation: student | **B. Inferable** | A T2202 proves it. Asking as well is redundant once documents exist. |
| Situation: rental income | **C. Conditional** | Duplicated by the income step. |
| Situation: investments | **C. Conditional** | Duplicated by the income step. |
| Situation: owns home | **C. Conditional** | Only matters for specific claims. |
| Situation: first-time home buyer | **D. Deferrable** | Nothing depends on it until an FHSA/HBP path opens. |
| Situation: disability | **C. Conditional** | Correctly opt-in and correctly hinted. |
| Kind of income (8 categories) | **B. Inferable** | A T4 states the type. This is the clearest document-eliminable question in the product. |
| Amount | **B. Inferable** | Box 14 of a T4. |
| Where it came from | **B. Inferable** | Employer name is on the slip. |
| Account type (RRSP/FHSA) | **C. Conditional** | Only if the taxpayer has one. |
| Contributed so far | **B. Inferable** | On the contribution receipt. |
| **Contribution room available** | **E. Unnecessary to ask this way** | Cannot be answered from memory; belongs on a Notice of Assessment. Asking a beginner to type it invites a wrong number into a governed calculation. |
| What the amount is for | **B. Inferable** | Receipt states it. |
| Amount paid | **B. Inferable** | Receipt states it. |
| Description | **D. Deferrable** | Optional metadata. |

**Counts for the persona (employee + student, one T4, one T2202):**

| Measure | Today | Achievable |
|---|---:|---:|
| Questions presented | 11 fields + 7 checkboxes | 2 questions + 2 confirmations |
| Fields requiring typing | ≥ 6 | 0–1 |
| Questions duplicated elsewhere | 2 (province, marital status) | 0 |
| Questions the situation step re-asks | 4 | 0 |
| Questions no beginner can answer | 1 (contribution room) | 0 |

**Duplicate data entry.** Province and marital status are collected in
`Onboarding.tsx` step 1 and again in `Settings.tsx`, with no indication to the
user that these are the same facts.

---

## F. Current time to first value

**First personalised value arrives at the end of a five-step form.**

Counted from account creation on the running product:

| | Count |
|---|---:|
| Screens before first value | 8 (landing → signup → verify → sign-in → legal accept → `/app` empty → onboarding ×5 steps → run) |
| Questions answered | 11+ |
| Fields typed | ≥ 6 |
| Documents | 0 (impossible — no upload exists) |
| Clicks (minimum) | ~25 |
| Estimated minutes | 8–15, longer if the user leaves to find a contribution-room figure |

Nothing personalised appears before that. The `/app` empty state is
identical for every account.

**The achievable target** — under the document-first model in §I — is *first
recognised fact within one interaction*: a T4 photographed and read back as
*"You worked at ACME. You earned $34,120."* That is value before any question
has been answered.

---

## G. Proposed product philosophy

1. **The engine's sophistication is the reason the surface can be simple.**
   Every simplification must be a *presentation* decision over an authority
   that already exists — never a new authority, and never a shortcut around one.
2. **Never invent a fact to save a question.** Where a document can supply the
   answer, read it and ask for confirmation. Where nothing can supply it,
   ask — and always offer "I don't know" as a first-class answer that leads
   somewhere.
3. **The customer sees a conclusion; the record keeps the proof.** Level 1
   language must never be the only record of a finding (§M).
4. **One obvious next thing, always.** The product already computes the order
   (§J). The customer should see the first item, not the queue.
5. **A technical term is never removed, only demoted.** Every plain-English
   default keeps its precise term one level down.
6. **Honesty outranks reassurance.** The existing empty-state copy is a model:
   it refuses to report an unanswered question as a clean result. Keep that.
7. **No gamification.** No streaks, no confetti, no countdowns, no artificial
   urgency. The existing product already resists this; it must continue to.

---

## H. Proposed information architecture

Today: `Overview · Tax position · Opportunities · Decision Twin · Evidence · What changed`
— six domain nouns, unranked.

Proposed:

| Area | Purpose | Contains | Must not contain | Maps to |
|---|---|---|---|---|
| **Home** | Where am I, what's next | the single next action, the headline number, a short "what we found" summary | the family ledger, raw codes, multiple competing CTAs | `/ioe/assurance` `attention[0]`, position headline |
| **What we need** | Everything blocking or unresolved, in the engine's order | evidence gaps, decisions, unanswered questions, each as one task | anything already resolved | `attention[]` filtered to `EVIDENCE_REQUIRED` / `DECISION_REQUIRED` |
| **What we found** | Findings in plain language | opportunities with Level 1 outcome + progressive disclosure | rule codes at Level 1 | `assurance.opportunities` |
| **Your numbers** | The position and its breakdown | income, tax, the calculation | assurance family internals | `/app/position` |
| **Documents** | Everything you've given us and what it produced | uploads, extraction confirmations, provenance | — | **`/api/v1/documents` — exists, unused** |
| **Ask Onyx** | Explanation on demand, in context | the existing explanation envelope, surfaced where the question arises | any generative tax authority | `POST /ai/explanations` |

"Decision Twin" and "Before you act" become a *mode* inside **What we found**
("See what would change") rather than a destination. "What changed" becomes a
returning-user banner on **Home**, not a sixth nav item.

---

## I. Minimum-Friction Tax Path — architecture and feasibility

**Definition.** Given everything Onyx knows, determine the smallest safe set of
additional questions, confirmations and documents needed for a complete,
defensible result.

**Feasibility: high, and most of it already exists.**

What is already there:

| Capability | Where | State |
|---|---|---|
| Deterministic, total ordering of what needs attention | `backend/app/services/state_graph/assurance.py:344` `_attention_key` | **Built** |
| Pure, total derivation of the whole assurance map | same file, `derive_assurance_map` — *"any well-formed CURRENT graph derives, and the same (graph, as_of) always derives the same map"* | **Built** |
| Closed action vocabulary driving what a customer can close | `ActionStatus`: `EVIDENCE_REQUIRED`, `DECISION_REQUIRED`, `ACTION_AVAILABLE`, `BLOCKED` | **Built** |
| Evidence-gap detection | `_EVIDENCE_GAP` = `MISSING` ∪ `PARTIAL` ∪ `UNKNOWN` | **Built** |
| Closed review-reason vocabulary | `GOVERNED_RE_EVALUATION_REQUESTED`, `INPUTS_CHANGED_SINCE_EVALUATION`, `ELIGIBILITY_INDETERMINATE` | **Built** |
| Document upload → ~~process → extract~~ → confirm into income/expense rows | `backend/app/api/v1/documents/routes.py` | **PARTIAL — upload and confirm are real; extraction from a stored document does not exist.** See `document-first-blocker.md` |
| Explanation with a correct authority boundary | `POST /ai/explanations` | **Built** |

What is missing — and all of it is presentation or mapping, not authority:

1. **A lexicon.** `code → { level1, level2, technical }` for every member of
   every closed enum. Testable for exhaustiveness (§N).
2. **A question projection.** A function from an evidence gap to a specific
   consumer request ("we need your tuition receipt"). This is a *lookup keyed
   by the gap the engine already reports*, not an inference.
3. **A documents UI.** The contract exists; nothing calls it.
4. **An "I don't know" path** that records unknown-ness as a first-class state
   rather than forcing a wrong value into a governed calculation.

**Where AI may and may not participate.** AI may phrase, explain and summarise
what the deterministic path produced. AI must not decide what the next question
is, because the next question follows from eligibility and evidence state,
which are governed. `_attention_key` stays the authority; the lexicon translates
its output; the model may narrate it.

**Invariant check.** Nothing above weakens deterministic authority, rule
versioning, evidence provenance, replay, RLS, tenant isolation or auditability:
the ordering, the eligibility and the evidence requirements are all read from
existing sealed outputs. A lexicon is a display map. A questions projection is
a display map. Neither can change a number.

---

## J. Next-Best-Action model

**Onyx already computes this.** `_attention_key` is documented as *"THE ordering
rule, in one place"*:

1. deadline urgency band — time pressure first, expired last
2. action band — gaps the customer can close (evidence, decisions) before items
   that are merely ready; blocked last
3. the optimizer's own sealed `candidate_rank` — *"material impact ordering is
   DELEGATED to the authority that already ranked it. This module never re-ranks
   by amount, which is what keeps it a presentation order rather than a second
   recommendation engine."*
4. identity, so the order is total

`/ioe/assurance` already returns `attention[]` in that order, and
`Overview.tsx` already renders it *"in the backend's own order"*.

**What changes is only what the customer sees:** today the queue is the second
panel and shows up to six items; it should be `attention[0]`, rendered as one
task with one button, with the rest available under "What else needs me".

**States** — all derivable today:

| State | Derivation |
|---|---|
| Blocking | `action = EVIDENCE_REQUIRED` or `DECISION_REQUIRED` |
| Optional | `action = ACTION_AVAILABLE` |
| Waiting on Onyx | family `UNAVAILABLE` with `reason_code` naming a run that has not happened |
| Blocked | `action = BLOCKED` |
| Deferred / skipped | **not modelled today** — the one genuinely new piece of state, and it is UI state, not tax state |
| Complete | absent from `attention[]` |

Resume behaviour needs nothing new: the queue is derived from current state on
every read, so a returning user gets the correct next action by construction.

---

## K. Beginner onboarding, screen by screen

Designed for the persona. Five steps become **three screens and two
confirmations**, and the first personalised fact arrives on screen 2.

### Screen 1 — Where you lived

- **Objective:** the two facts nothing can infer.
- **Heading:** "First, where did you live?"
- **Copy:** "Your province decides which rules apply to you."
- **Inputs:** province (select, default from nothing — never guessed);
  "Did your relationship status change in 2025?" → Single / Married or
  common-law / It changed / **I'm not sure**.
- **Primary CTA:** Continue. **Secondary:** none.
- **"I don't know":** records `UNKNOWN`; the engine treats marital status as
  unresolved and the fact appears in *What we need*. It does not guess.
- **Mobile:** two controls, thumb-reachable, sticky CTA.
- **A11y:** native `<select>`, one `<h1>`, focus moves to the heading on step
  change — the existing onboarding already does this
  (`Onboarding.tsx` focus management), and it should be kept.
- **Done when:** province is set.

### Screen 2 — Your documents

- **Objective:** first value, before any tax question.
- **Heading:** "Do you have any tax slips?"
- **Copy:** "Take a photo of anything you've received — a T4 from a job, a
  T2202 from school. We'll read it and show you what we found. You can skip
  this and type things in instead."
- **Inputs:** camera / file upload (`POST /api/v1/documents`).
- **Primary CTA:** Take a photo. **Secondary:** "I don't have any yet".
- **What the system already knows:** nothing yet — this is the earliest point
  it can know something.
- **"I don't know":** skipping routes to the manual path, not a dead end.
- **Mobile:** camera capture is the *primary* input, not a fallback.
- **Done when:** at least one document processed, or explicitly skipped.

### Screen 3 — Here's what we found

- **Objective:** confirmation replaces data entry.
- **Heading:** "Is this right?"
- **Copy:** "We read your T4. Check these numbers match your slip."
- **Inputs:** read-back fields from `POST /documents/{id}/process`, each
  editable; confirm writes rows via `POST /documents/{id}/confirm`.
- **Primary CTA:** "Yes, that's right". **Secondary:** "Something's wrong".
- **Level 2 disclosure:** "This is employment income (box 14)."
- **"Something's wrong":** opens the single field to correct, not the whole form.
- **Done when:** confirmed or corrected.

### Screen 4 — Anything else about your year

- **Objective:** the conditional questions, asked only when they can matter.
- **Heading:** "Anything else happen in 2025?"
- **Copy:** plain-language life events, **not** tax categories: went to school ·
  worked for yourself · paid rent · moved for work or school · had medical costs
  · gave to charity · **I'm not sure**.
- **Note:** every option here that a document already proved is **pre-ticked and
  shown as already known**, not asked again.
- **"I'm not sure":** selects nothing and proceeds; unresolved items surface
  later in *What we need* rather than blocking now.
- **Done when:** continue is pressed — no minimum selection.

### Screen 5 — Your position

- Not a step. The result, with the next action on it.

**What is deliberately not asked:** income type, income amount, employer name,
contribution room, contributed-so-far — all either read from a document or
deferred to a later, targeted question with an "I don't know" branch.

---

## L. Return home screen

Answers the brief's six questions in this order:

```
Where you stand                                   ← plain heading, no year chrome
Your estimated position          $X,XXX           ← Level 1, one number, stated once
                                 Why this number →

Next                                              ← attention[0], ONE task
  We need your tuition receipt
  [ Add it ]        Not now

What we've found                                  ← counts, not a ledger
  3 things that could help you       See them →
  1 we're still checking

Anything else needing you        2 →              ← the rest of attention[]
```

**States.** Nothing yet → a single "Add your first document" card and nothing
else. Everything resolved → "Nothing is waiting on you" (the existing copy is
already right). Something changed since last visit → one banner above Next,
sourced from `/app/changes`.

**Removed from the home screen:** the ten-family assurance ledger (moves to
Level 4 under "Why this number"), the "Re-run analysis" button (analysis
re-runs when inputs change; a manual re-run belongs in settings or a menu), the
global tax-year selector for accounts with one year.

---

## M. Progressive disclosure — the reusable pattern

One component, four levels, used for opportunities, exclusions, missing
evidence, recommendations, scenario comparisons, calculations and warnings.

| Level | Answers | Example | Source |
|---|---|---|---|
| **1 — Outcome** | What is it worth to me? | "Putting money in an RRSP could lower your tax by about **$240**." | `candidate_rank`, projected effect |
| **2 — Why me** | Why does this apply? | "You earned employment income, and you have room to contribute." | eligibility result + `opportunity_code` lexicon |
| **3 — Calculation** | How was it worked out? | inputs, arithmetic, the figures used | sealed calculation |
| **4 — Proof** | Can I check it? | formal term, rule code, **rule version**, evidence, assumptions, source | governed rule + provenance |

**Rules.** Level 1 is always plain language and never a code. Level 4 is always
reachable in one action from Level 1 and never more than one action away. A
finding with no Level 4 is a defect, not a simplification. Levels 3 and 4 are
the existing `Provenance` and `TrustPair` components' job
(`frontend/src/components/trust.tsx`) — they already exist and are already good;
this pattern gives them a consistent home.

---

## N. Human-language translation layer

**The mechanism.** A single lexicon module mapping every member of every closed
vocabulary to `{ level1, level2, technical }`. Not scattered ternaries — one
map, with an exhaustiveness test.

Vocabularies to cover (all closed today):

- `ActionStatus` — 4 members
- `UrgencyStatus` — 5 members
- `EvidenceReadiness` — `MISSING`, `PARTIAL`, `UNKNOWN`, …
- assurance family status — 6 members (`READY`, `EVIDENCE_REQUIRED`, `REVIEW_REQUIRED`, `BLOCKED`, `UNAVAILABLE`, `NOT_APPLICABLE`)
- review reason codes — 3 members
- assurance families — 10 members
- opportunity codes — enumerable from published rules
- error and failure codes reaching the UI

Sample:

| Code | Level 1 | Level 2 | Technical |
|---|---|---|---|
| `EVIDENCE_REQUIRED` | "We need a document" | "We can't confirm this without seeing it." | Evidence required |
| `DECISION_REQUIRED` | "We need you to choose" | "There's more than one way to do this." | Decision required |
| `ELIGIBILITY_INDETERMINATE` | "We can't confirm this yet" | "One more detail decides it." | Eligibility indeterminate |
| `INPUTS_CHANGED_SINCE_EVALUATION` | "Something changed" | "We'll take another look." | Inputs changed since evaluation |
| `UNAVAILABLE` + `NO_OPTIMIZATION_RUN` | "We haven't looked at this yet" | "Nothing to report until we do." | Not established — no optimization run |
| `NOT_APPLICABLE` + `RESERVED_NO_PRODUCER` | *(do not display)* | — | Reserved — no producer |
| `BLOCKED` | "This can't go ahead" | "Something earlier has to be resolved first." | Blocked |
| `EXPIRED` | "The deadline has passed" | "This one is no longer available for 2025." | Expired |

**The test that makes this safe:** a test asserting every enum member has a
lexicon entry, so adding a backend code without a consumer string fails CI
rather than reaching a customer as a de-underscored identifier. This is the
same shape as the structural guard added for `run_task` — a rule that cannot go
stale, rather than a list someone must remember to update.

---

## O. AI explanation experience

**The authority boundary is already correct and is documented in the client:**

> *"The browser never talks to a model provider: it names a subject it already
> owns, and the backend assembles the input, runs its validators and returns
> validated output (or its deterministic renderer's). No provider credential
> exists in this bundle because no provider call is made from it."*
> — `frontend/src/api/endpoints.ts`

Keep every part of that. The problem is not authority, it is **placement**:
five collapsed "Explain this…" buttons, one per surface, each requiring the
user to already be on the right screen and to know the button will help.

**Proposed.** Explanation becomes ambient rather than a destination:

- Every Level 1 statement carries "Why?" inline, opening Level 2 from the same
  envelope.
- Wherever a question is asked, "Why do you need this?" is available — the
  onboarding's existing "Why we need this" disclosure is the right pattern and
  should be everywhere, not only in onboarding.
- No free-text chatbot on the tax surface. If a conversational entry point is
  added, it answers **only** about subjects the account already owns, through
  the same envelope, and every answer carries its Level 4 link.

**Must not, restated as testable prohibitions:** the model may not originate an
eligibility determination, a tax figure, a rule citation, or a document
requirement; it may not write to filing data; and an uncertain result must not
be rendered as certain.

---

## P. Mobile

Baseline is better than expected: the stylesheet is **mobile-first**
(`min-width` breakpoints at 48rem and 64rem), and `prefers-reduced-motion`,
`prefers-color-scheme` and print are all handled. The E2E suite runs every
journey at a Pixel 7 profile.

Critical changes:

1. **Camera capture as the primary document input** — the entire §K screen 2
   depends on it, and it is the single interaction that makes a phone better
   than a laptop for this product.
2. **Sticky primary CTA** on every step screen; a beginner should never have to
   hunt for "continue".
3. **The assurance ledger is a 10-row table on a phone.** It must not be the
   home screen's third panel; at Level 4 it needs a card layout, not a table.
4. **One question per screen** on the onboarding path — today step 1 has a
   select, a select and seven checkboxes.
5. **Currency inputs must open a numeric keypad** and format on blur.
6. **Scenario comparison tables** (`DecisionTwin`, `BeforeYouAct`) need a
   stacked before/after presentation on narrow screens.

---

## Q. Accessibility

The posture is genuine, not decorative, and several patterns should be treated
as the standard the redesign must meet:

- an axe sweep over authenticated surfaces (`frontend/e2e/a11y.spec.ts`)
- focus moves to the step heading on navigation (`Onboarding.tsx`)
- the visual assurance ledger is `aria-hidden` and **paired with a real table**
  carrying the same facts for assistive technology (`Overview.tsx`) — an
  unusually good decision
- "Skip to main content" on every page
- `prefers-reduced-motion` honoured

Critical changes:

1. **Do not let minimalism remove the paired table.** If the ledger moves to
   Level 4, its accessible equivalent moves with it.
2. **"I don't know" must be a real control**, not a link styled as text —
   it will be the most-used escape hatch in the product.
3. **Progressive disclosure needs disclosure semantics** — `aria-expanded`,
   focus management into the revealed level, and a return path.
4. **Level 1 must not be the only accessible text.** If a number is announced
   in plain language, the technical value must be reachable, not visual-only.
5. **Document capture needs a non-camera path** and clear labelling of what was
   extracted, for screen-reader users confirming a read-back.
6. **Status must never be colour-only** — the existing `status status--*`
   classes pair colour with a text label; keep that rule.

---

## R. Golden journey — employee + student, first-time filer

Assumptions: Canadian, Ontario, one T4, one T2202, no complexity.

| # | Screen | Shown | User does | Inferred | Deterministic | AI | If unsure | Progress |
|---|---|---|---|---|---|---|---|---|
| 1 | Create account | email, password | types 2 fields | — | — | no | — | — |
| 2 | Verify email | "Check your email" | clicks the link | — | — | no | resend | — |
| 3 | Accept terms | current documents | accepts | — | registry decides which | no | must accept | — |
| 4 | Where you lived | province, relationship | 2 taps | — | — | no | "I'm not sure" → recorded unknown | 1 of 3 |
| 5 | Your documents | camera prompt | photographs T4 | — | extraction | no | skip → manual path | 2 of 3 |
| 6 | **Here's what we found** | "You worked at ACME. You earned $34,120." | confirms | employer, amount, **income type** | `POST /documents/{id}/confirm` writes the rows | narrates only | "Something's wrong" → edit one field | **first value** |
| 7 | Your documents (again) | "Anything else?" | photographs T2202 | — | extraction | no | skip | 2 of 3 |
| 8 | Here's what we found | "You paid $4,210 in tuition at …" | confirms | tuition amount, student status | confirm writes rows | narrates | edit | — |
| 9 | Anything else | life events; *school already ticked and shown as known* | 0–1 taps | student inferred from T2202 | — | no | "I'm not sure" → proceed | 3 of 3 |
| 10 | **Your position** | "$1,180 estimated refund" + Level 1 findings | reads | — | analysis + IOE | explains on demand | "Why?" → Level 2 | complete |
| 11 | Next | `attention[0]` as one task | acts or defers | — | `_attention_key` | phrases it | "Not now" → deferred | — |

**Totals:**

| | Golden journey | Today |
|---|---:|---:|
| Screens to first value | **6** | 8 |
| Screens to result | **10** | 11 |
| Manual answers | **2** (province, relationship) | 11+ |
| Typed fields | **0** (beyond email/password) | ≥ 6 |
| Document interactions | 2 | 0 (impossible) |
| Confirmations | 2 | 0 |
| Questions no beginner can answer | **0** | 1 |

The reduction comes almost entirely from **two** changes: surfacing the
document pipeline that already exists, and not asking again for what a document
already proved.

---

## S. Existing persona compatibility

The suite covers 12 personas (`frontend/e2e/journeys.spec.ts`): `employee`,
`employee-medical`, `donations`, `high-income-donation`, `se-below-cpp2`,
`se-in-cpp2`, `se-above-yampe`, `mixed`, `multi-source`, `rrsp-contributor`,
`fhsa-contributor`, `both-registered`.

| Proposed element | Universal | Persona-specific | Notes |
|---|---|---|---|
| Lexicon / plain-language Level 1 | ✅ | — | benefits every persona |
| Next-best-action home | ✅ | — | derived from existing ordering |
| Progressive disclosure | ✅ | — | Level 4 is what advanced personas need |
| Document-first intake | ✅ | partial | `se-*` personas have no slip for business income — the manual path must remain first-class, not a fallback |
| Life-event question set | ✅ | — | must not *replace* the ability to enter a category directly |
| Hiding the assurance ledger | ⚠️ | `multi-source`, `high-income-donation` | these personas benefit from the family view; it must be reachable, not deleted |
| Removing the tax-year selector | ⚠️ | returning users | keep it for accounts with more than one year |

**The rule:** *beginner by default, depth on demand.* Every advanced surface
that exists today must remain reachable at Level 3/4. The value-parity tests
must keep passing unchanged — they assert the rendered figure equals the
engine's figure, and no language change may alter a figure.

---

## T. UX metrics

Instrumentable without weakening privacy — all are counts and timings of
interactions, none require recording tax content.

| Group | Metric | Source |
|---|---|---|
| Activation | signup → first document or first fact | client event + existing audit timestamps |
| **Time to first value** | account creation → first confirmed extraction or first position | derivable from existing rows |
| Completion | onboarding completion; position reached | route events |
| Friction | questions per completed position; typed fields; screens; back-navigations; validation errors; **"I don't know" rate** | client events |
| Automation | % of fields prefilled from documents; documents recognised; questions eliminated by evidence | `documents/{id}/confirm` created-row counts vs manual writes |
| Understanding | Level 2/3/4 opens per finding; repeated explanation requests | explanation calls per subject |
| Trust | correction rate on read-backs; overrides; evidence views | confirm-with-edit vs confirm-as-read |
| Performance | interaction latency; document processing wait; time to meaningful feedback | existing worker timings |

**Privacy constraint:** metrics must count *events*, never carry tax values.
The repository already forbids financial payloads in logs; the same rule binds
analytics.

---

## U. User-testing protocol

5–10 participants, none tax-expert, at least 3 who have never filed.

**Setup.** Real device, participant's own phone where possible. No tutorial, no
explanation of the interface. Screen and audio recorded with consent. Synthetic
documents only — never a participant's real slips.

**Tasks.**
1. "You've just got a job and someone told you to sort your taxes. Start."
2. "You have this T4. Do whatever you think you're supposed to do with it."
3. "Tell me what Onyx thinks your situation is."
4. "Onyx says it needs something. Sort that out."
5. "Do you think this number is right? How would you check?"

**Observers record:** hesitation >3s; questions asked aloud; back-navigation;
wrong selection; missed primary CTA; abandonment; any tax term read aloud with
uncertainty; whether the participant can say what to do next; whether they can
say what Onyx found; whether they believe the number.

**Success criteria for the golden journey:**
- ≥ 4 of 5 reach a position without assistance
- 0 participants required to know a tax term to proceed
- ≥ 4 of 5 can state the next action unprompted
- 0 participants type a number they were unsure of
- median time to first value < 3 minutes

---

## V. Implementation roadmap

Ordered by impact on abandonment, then cognitive load, then time-to-value,
against engineering cost and risk. **Phases 0 and 1 are valuable under either
answer to the A1 product question.**

| Phase | Scope | Abandonment | Cognitive load | TTFV | Eng. cost | Tax risk | Sec/priv risk | Depends on A1? |
|---|---|---|---|---|---|---|---|---|
| **0** | **Product decision: filing product or assurance product** | — | — | — | — | — | — | **is the decision** |
| **1** | Lexicon + Level 1/2 language + exhaustiveness test | High | **Highest** | — | Low | **None** — display only | None | No |
| **2** | Next-best-action home; demote the family ledger to Level 4 | **Highest** | High | Medium | Low–Med | None — reuses `_attention_key` | None | No |
| **3** | Documents UI over the existing contract; read-back confirmation | High | High | **Highest** | Medium | Low — `confirm` already governs writes | Medium — file handling, already designed | No |
| **4** | Onboarding rebuild: life-events, "I don't know", no un-answerable fields | High | High | High | Medium | Low | None | No |
| **5** | Progressive-disclosure component across all finding types | Medium | High | — | Medium | None | None | No |
| **6** | Ambient explanation placement | Medium | Medium | — | Low | None | None | No |
| **7** | Mobile capture + one-question-per-screen | High | Medium | High | Medium | None | None | No |
| **8** | Complex-taxpayer depth: keep every advanced surface at Level 3/4 | Low | — | — | Medium | None | None | No |
| **9** | Return model, completeness, filing pathway | — | — | — | **Very high** | **High** | High | **Yes — only if filing** |

Phase 1 before Phase 2 is deliberate: a next-best-action card that renders
*"Evidence required: Increase rrsp deduction"* is not an improvement.

---

## W. First recommended implementation entry

**Entry: the consumer lexicon and Level 1/2 language layer.**

Chosen because it is the highest cognitive-load win, has **zero** tax,
security and privacy risk, is independently testable, is fully reversible, and
is a prerequisite for every later phase — and because it does not depend on
resolving the A1 product question.

**In scope**

- A new frontend lexicon module: `code → { level1, level2, technical }` for
  `ActionStatus`, `UrgencyStatus`, `EvidenceReadiness`, assurance family status,
  assurance family names, and the three review reason codes.
- Replace `humanize()` at consumer-facing call sites in `pages/Overview.tsx`
  (`:52, :84, :107, :144, :147, :158`) and `pages/Opportunities.tsx` with
  lexicon lookups.
- Suppress display of codes that describe Onyx's architecture rather than the
  customer's tax — `RESERVED_NO_PRODUCER` and equivalents.
- Attach the technical term as the Level 2/4 value wherever a Level 1 string
  replaces it, using the existing `Provenance`/`TrustPair` components.

**Backend contracts involved:** read-only. `/ioe/assurance`
(`TaxAssuranceOut`), `/ioe/opportunity-lifecycle`. **No backend change.**

**Explicit non-goals**

- No navigation change. No home-screen restructuring. No documents UI.
- No onboarding change. No new API. No schema or migration.
- No change to any figure, ordering, eligibility or rule.
- `humanize()` is **not** deleted — it remains correct for genuinely
  presentational strings such as field names in advanced tables.

**Required tests**

1. Exhaustiveness: every member of every covered enum has a lexicon entry —
   fails CI when a backend code is added without a consumer string.
2. No consumer surface renders a raw `SCREAMING_SNAKE_CASE` identifier
   (structural, AST or DOM-level, in the spirit of the `run_task` guard).
3. Suppressed codes never reach the DOM.
4. Every Level 1 string has a reachable technical term.
5. Existing a11y sweep and the 12-persona value-parity journeys pass unchanged.

**Acceptance criteria**

- A first-time filer's empty `/app` shows no internal reason code.
- Every attention item's title and subtitle are plain language.
- Every replaced term is reachable at Level 2 or Level 4 in one action.
- Value-parity tests unchanged and passing — **no figure moved**.

**Regression requirements**

- Full backend suite unchanged (no backend code is touched).
- Frontend unit + a11y + all 12 persona journeys green.
- Release gate green on the final SHA.

**Rollback boundary**

One frontend module and a bounded set of call sites, behind no feature flag and
touching no persisted state. Reverting the commit restores the prior strings
exactly; nothing is migrated, so there is nothing to un-migrate.

---

## Invariant compliance

Every recommendation in this document preserves, and none may be implemented in
a way that weakens: deterministic tax-calculation authority; tax correctness;
rule versioning; evidence provenance; replay integrity; privacy isolation;
tenant isolation; RLS; authorization; security boundaries; auditability; data
integrity; regulatory controls; immutable sealed evidence.

**Two simplifications were considered and rejected on those grounds:**

1. *Inferring marital status or province from other data to remove two
   questions.* Rejected — both change which governed rules apply, and a guessed
   jurisdiction produces a defensible-looking figure computed under the wrong
   rules. The existing onboarding copy already makes this argument, correctly.
2. *Letting the explanation model phrase an eligibility outcome when the
   deterministic renderer has no string for it.* Rejected — that is a model
   originating an eligibility statement. The lexicon must be exhaustive
   instead, which is why §W's first test is exhaustiveness.

---

# Appendices

The required report is sections A–W. These three appendices carry analysis the
brief asked for (its §§7, 17, 21, 27) that does not belong inside a lettered
section.

## Appendix 1 — Screen inventory

Every consumer-facing route, against the brief's fifteen questions. Condensed
to the answers that differ from "no" or "unchanged".

| Route / file | Why it exists | Customer's job | What Onyx needs | What Onyx could already know | Tax knowledge required | Primary action | Obvious? | Competing actions | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| `/` `Landing` | Explain the product | Decide whether to sign up | nothing | — | none | Create account | Yes | Trust Centre, AI transparency | **Keep** |
| `/sign-up` | Account | Sign up | email, password | — | none | Create account | Yes | — | **Keep** |
| `/verify-email` | Prove the address | Click a link | — | — | none | (automatic) | Yes | — | **Keep** |
| `/legal/accept` | Consent | Accept | — | which documents are outstanding | none | Accept | Yes | — | **Keep** |
| `/app` `Overview` | Where you stand | Know status and next step | — | position, attention order, families | **high** — 10 family names, reason codes | ambiguous: "Add your details" vs "Re-run analysis" | **No** | 4 panels + 4 links | **Restructure** (§L) |
| `/app/onboarding` | Collect facts | Get to a result | province, marital status, income, claims | income type/amount/employer **from a document** | **high** | Save and continue | Yes | — | **Rebuild** (§K) |
| `/app/position` | The numbers | Understand the figure | — | everything | medium | none — read-only | n/a | Add your details | **Keep**, becomes "Your numbers" |
| `/app/opportunities` | Findings | Decide what to act on | decisions, evidence | eligibility, ranking | **high** | per-item | Partly | 11 inputs | **Keep**, apply §M |
| `/app/twin` `Decision Twin` | Model a change | "What if I did X" | lever inputs | scenarios | **high** | run a scenario | Partly | 6 inputs | **Rename + demote** to a mode |
| `/app/before-you-act/:id` | Pre-commitment check | Confirm before acting | — | comparison | **high** | review | Yes | — | **Keep**, apply §M |
| `/app/evidence` | What's missing | Close gaps | documents | readiness per requirement | **high** | none actionable — **there is no upload** | **No** | — | **Merge** into "What we need" + Documents |
| `/app/changes` | What moved | Catch up | acknowledgement | change set | medium | I have reviewed this | Yes | Explain what changed | **Demote** to a Home banner |
| `/app/settings` | Account | Manage profile/privacy | province, marital status *(duplicates onboarding)* | — | low | Save tax profile | Yes | Sign out, Delete account | **Keep**, de-duplicate |
| `/trust` `TrustCentre` | Earn trust | Decide whether to believe it | — | — | low | read | Yes | many links | **Keep — exemplary** |
| `404` | — | Recover | — | — | none | back | Yes | — | **Keep** |

**Screens that can be removed or merged:** `/app/evidence` merges into "What we
need" once evidence gaps become tasks; `/app/changes` becomes a Home banner;
`/app/twin` becomes a mode inside a finding rather than a destination. That
takes primary navigation from six items to four or five.

**"I don't know" handling today: there is none, on any screen.** That is the
single most consequential omission for the persona, because the alternative a
nervous filer chooses is to guess.

## Appendix 2 — Uncertainty and error language

Current states, classified. The distinction that matters is whether the
customer can do anything, and whether their **tax result is affected** — the
second question is the one current messages never answer.

| State | Class | Says what happened? | Says if the result is affected? | Says what to do? | Proposed |
|---|---|---|---|---|---|
| `EVIDENCE_REQUIRED` | User-recoverable | code only | no | no | "We need your {document}. Until then this isn't counted." + upload |
| `DECISION_REQUIRED` | User-recoverable | code only | no | no | "There's more than one way to do this. Pick one and we'll work it out." |
| `ELIGIBILITY_INDETERMINATE` | User-recoverable | code only | no | no | "One more detail decides whether this applies to you." + the detail |
| `INPUTS_CHANGED_SINCE_EVALUATION` | System-recoverable | code only | **no — and it is** | no | "Something changed, so this number may move. We're re-checking." |
| `GOVERNED_RE_EVALUATION_REQUESTED` | System-recoverable | code only | no | no | "We're taking another look." — no user action implied |
| `UNAVAILABLE` + no run | System-recoverable | yes, in system terms | partly | no | "We haven't worked this out yet." |
| `BLOCKED` | Mixed | code only | no | **no — worst case** | "This can't go ahead until {prerequisite}." |
| `EXPIRED` | Not recoverable | code only | no | no | "The deadline for this passed on {date}." |
| `NOT_APPLICABLE` / `RESERVED_NO_PRODUCER` | Not a state the user has | leaks architecture | n/a | n/a | **do not display** |
| `RULES_EVALUATION_FAILED`, `PORTFOLIO_ASSEMBLY_FAILED`, `NORMALIZATION_FAILED`, `PERSISTENCE_FAILED` | Technical / support | leaks internals | no | no | "Something went wrong on our side. Your information is safe and nothing was lost. We're looking at it." + reference id |
| Stale freshness | System-recoverable | partly | partly | no | "This was worked out before your last change." |

**Four things every message must answer**, none of which the current codes do:
what happened; whether the tax result is affected; what the customer should do;
whether anything was lost. The last is the one that determines whether a
nervous first-time filer stays.

## Appendix 3 — Visual principles, and what is *not* a UX fix

**Principles the eventual design system must satisfy** — assessed against what
exists today (`frontend/src/styles/`: `tokens.css`, `base.css`, `product.css`,
`shell.css`, `trust.css`).

| Principle | Today | Note |
|---|---|---|
| Generous whitespace | **Good** | the captured screens are calm and uncrowded |
| Strong typography | **Good** | serif headings, clear hierarchy |
| Restrained colour | **Good** | near-monochrome with one accent |
| Obvious hierarchy | **Mixed** | typography is right, *ordering* is wrong (§L) |
| One primary CTA | **Fails** | Home offers "Re-run analysis" and "Add your details" |
| Low visual noise | **Fails on Home** | ten zero-item ledger rows |
| Consistent components | **Good** | `states.tsx`, `trust.tsx` are reused properly |
| Meaningful states | **Good** | empty/loading/error states exist and are honest |
| Clear progress | **Partial** | onboarding has a step indicator; nothing else does |
| No decorative filler | **Good** | there is none |
| Trusted-financial feel | **Good** | this is the product's strongest visual asset |
| No accountant-dashboard aesthetic | **Fails** | the assurance ledger is exactly that |
| No AI-dashboard aesthetic | **Passes** | |
| No crypto/trading aesthetic | **Passes** | |
| No childish gamification | **Passes** | and must stay that way |

**Explicitly cosmetic — these do not solve any friction in this report:**
border radii, shadows, gradients, icon sets, accent-colour changes, animation,
font swaps, spacing scales, dark-mode palette tuning.

**Not cosmetic, despite looking visual:** demoting the assurance ledger
(removes ten unexplained concepts from the first screen); making the next
action the largest element (answers "what do I do"); one question per screen on
mobile (reduces decisions per screen); the disclosure component (makes depth
reachable without making it default). Each changes comprehension or task
completion, which is the test the brief sets.

**The order of work is: language, then hierarchy, then automation, then
visuals.** A visual pass before §W's lexicon would produce a beautifully
typeset screen that still says "Reserved no producer".
