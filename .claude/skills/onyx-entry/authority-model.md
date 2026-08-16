# Authority model

Onyx Ledger calculates numbers people act on. The organising principle is that
**exactly one governed authority owns each material decision**. When two places
can answer the same question, they will eventually disagree, and the disagreement
will surface as a wrong number in front of a user who trusted it.

## The authority inventory

Discover the current set from the repository — this table names the boundaries,
not a frozen file list.

| Decision | Sole authority |
|---|---|
| Tax calculation | `app/services/tax_engine/service.py` (`TaxEngineService`) |
| Rule and eligibility evaluation | `app/services/tax_engine/rules_service.py` (`RulesEvaluatorService`) |
| Canonical serialization and hashing | `app/services/ioe/domain/canonical.py` |
| Scenario-result protocol versions | `app/services/ioe/domain/scenario.py` |
| Privacy classification of every table | `app/privacy/classification.py` |
| Graph node/edge canonical payloads and hash | `app/services/state_graph/hashing.py` |
| Sealed historical source loading | `app/services/ioe/scenario/historical_source.py` |
| Comparability policy for projections | `app/services/state_graph/scenario_projection.py` |

Extend this by inspection when an entry introduces or touches an authority.

## Rules

**Never duplicate tax calculation outside `TaxEngineService`.** Not "just this
once for a preview", not "a quick estimate for the UI". A second implementation
is a second answer.

**Never derive tax amounts heuristically** from unrelated outputs. Subtracting
two figures that a governed authority already sealed is a comparison and is
fine. Inferring a figure nobody computed is fabrication.

**Never duplicate eligibility logic outside `RulesEvaluatorService`.**

**Never use an LLM as an authority** for tax amounts, eligibility, rules,
evidence requirements, deadlines, citations, or support/confidence scores.

**Never let presentation decide business facts.** API serializers, formatters
and frontend code render governed state; they do not determine it.

**Never create a parallel implementation for convenience.** If calling the
authority is awkward, fix the seam or extract the shared helper from the
authority — do not clone it.

## Live vs sealed state

Three distinct concepts that must not collapse:

**Live state** — current financial data, latest published rules, current
documents, mutable recommendation state. What is true *now*.

**Frozen / sealed historical state** — what a scenario or analysis was actually
sealed with: pinned rule versions, frozen input snapshots, sealed results. What
was true *then*, and still reproducible.

**A historical read must never silently reconstruct sealed results from current
business state.** If an implementation is reading historical state and finds
itself reaching for current financials, the latest rules, current documents, or
unpinned optimizer output, that is a design error to investigate, not a fallback
to accept. The seal exists precisely so the answer cannot drift.

## Freshness is not integrity

These are separate questions with separate answers, and collapsing them into a
single status destroys both:

**Freshness** — do newer inputs, rules or versions make an old result unsuitable
as a *current recommendation*? A perfectly reproducible result can be stale.

**Integrity** — can the historical result still be reproduced and verified from
the evidence and versions it was actually sealed with? A perfectly current-looking
result can be unverifiable.

A result can be fresh and broken, or stale and perfectly sound. Report both.

## Source authority semantics

`SourceAuthority` (in `historical_source.py`) distinguishes four states that
downstream code will otherwise flatten into "zero rows":

| State | Meaning |
|---|---|
| `AUTHORITATIVE` | Loaded from a pinned frozen source; holds content |
| `AUTHORITATIVE_EMPTY` | Loaded from a pinned frozen source that genuinely holds nothing |
| `MISSING_AUTHORITY` | No frozen source exists to load this family from |
| `NOT_APPLICABLE` | The family does not apply in this context at all |

**Empty is not missing.** If an authoritative source was consulted and
legitimately contains zero items, that is authoritative emptiness and is safe to
compare. If no authoritative source exists or was captured, *do not infer zero*.

Never manufacture `ADDED`, `REMOVED`, `UNCHANGED`, eligibility, ineligibility,
evidence readiness, or resource state out of missing authority. Fail closed
where the domain contract requires it — a refusal is honest; a diff built on an
unloaded family is a false statement about someone's tax position.

## Resource semantics

Portfolio resource semantics must not be fabricated in single-scenario contexts.
Where the architecture marks `RESOURCE` as
`NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON`, preserve that verdict exactly.

Do not translate "not applicable" into zero capacity, zero allocation, an empty
resource list, or a removed resource. Those are claims; N/A is the absence of a
claim. Only a governed architecture change may alter this.

## AI explanation boundary

The AI layer is a **renderer over governed structured output**. It explains what
the authorities decided; it does not decide.

An LLM must not independently determine tax amounts, eligibility, rules, evidence
requirements, deadlines, citations, support or confidence scores, or authoritative
recommendation state.

Where AI-generated text conflicts with structured authoritative state, **the
structured state wins** — and the conflict is a defect worth reporting, not a
presentation preference.
