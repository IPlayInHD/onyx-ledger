# OPPORTUNITY LIFECYCLE (Entry: Opportunity Expiry / Decay + Lifecycle)

The deterministic current-state lifecycle of every governed opportunity: one
join of two certified read models, and the API that serves it.

```
GET /api/v1/ioe/opportunity-lifecycle?tax_year=YYYY[&as_of=YYYY-MM-DD]
```

```
TaxStateGraphService.build(tax_year)          certified, Entry 12A
    └── derive_assurance_map(graph, as_of=…)  certified, Tax Assurance Map
DecisionJournalService.list_journals()        certified, Tax Decision Journal
    └── project(events)                       certified, per thread
            └── derive_opportunity_lifecycle(assurance, threads, as_of=…)
                    │                         pure, no I/O, no clock
                    └── lifecycle_detail(…)   pure serializer
                            └── OpportunityLifecycleOut     contract v1.0.0
```

## 1. What it adds, and what it must never become

It answers eleven product questions — is this still available, does it need a
decision, did the user defer or decline, did they report acting, is evidence
still missing, is the window closing, has it closed, what stays visible after
it closes, what deserves attention — **without deciding any of them itself**.

Every value it reports was determined by an authority that owns it. The
contribution is the *join*, a documented *precedence*, and a closed
*actionability* vocabulary. It computes no tax, evaluates no rule, runs no
optimizer, reads no document, and adds no recommendation authority.

## 2. Seven axes, deliberately not one enum

Availability, decision, execution, evidence, timing, freshness and integrity
answer different questions. Collapsing any two makes a true combination
inexpressible, and the inexpressible combinations are exactly the ones a
customer needs:

- *urgent but already reported* — timing and execution disagree, both true
- *expired but the user acted* — the window closed after they used it
- *declined but still available* — the user said no; eligibility did not
- *reported but evidence missing* — §8's central case, see below

| Axis | Authority | Vocabulary |
|---|---|---|
| `availability` | rules `eligibility_status` + Assurance `BLOCKED` | AVAILABLE / CONDITIONALLY_AVAILABLE / UNDETERMINED / BLOCKED |
| `decision` | Decision Journal projection | NO_DECISION + the Journal's own four |
| `execution` | Decision Journal projection, verbatim | NOT_REPORTED / USER_REPORTED |
| `evidence` | Assurance `evidence_readiness`, verbatim | READY / PARTIAL / MISSING / NOT_REQUIRED / UNKNOWN |
| `timing` | Assurance `UrgencyStatus`, verbatim | NO_DEADLINE / NORMAL / APPROACHING / URGENT / EXPIRED |
| `freshness` | Assurance item freshness, verbatim | `NodeFreshness` |
| `integrity` | Assurance item integrity, verbatim | the integrity vocabulary |

Family-level absence is **not** an axis. "No governing run exists" is a
statement about the source, not about one opportunity, so it lives on
`opportunity_authority` — which is why an empty `opportunities` list is never
readable as "you have no opportunities".

## 3. Timing is read, never recomputed (decision A)

`URGENT_WITHIN_DAYS` and `APPROACHING_WITHIN_DAYS` are already governed by the
Assurance module, which evaluated them against the same `as_of`. This module
reads the verdict. A second implementation would be a second answer to "is this
urgent", and the two would eventually disagree.

There is therefore **no threshold literal anywhere in the lifecycle domain**,
and a test parses the module and fails if one appears — checked over the AST
rather than the text, so the docstring stays free to explain the delegation and
so `1 + 13` cannot slip past a substring sweep.

`as_of` is injected at the route boundary and carried through for echo and
provenance. Nothing in the domain reads a clock.

## 4. Expiry means a governed deadline passed — and nothing else

`EXPIRED` is Assurance's verdict on a governed deadline. It is never inferred
from inactivity, DEFER, DECLINE, missing evidence, staleness, absence from a
portfolio, or elapsed time since a simulation. With no governed deadline the
timing is `NO_DEADLINE` — not `OPEN_FOREVER`, and not `EXPIRED`.

Expired opportunities **stay in the response** with `timing = EXPIRED`. A
customer must be able to tell a window that closed from one that never existed,
and deletion destroys that distinction. They keep their governed figures; they
are not tombstones.

No "you missed $X". That would be a calculation no authority performed.

## 5. No decay score (decision B)

"Decay" is represented by three transparent facts — the governed `deadline`,
the exact `days_remaining`, and the deterministic timing band — plus seven
independent `AttentionFlags`.

A numeric `decay_score` / `opportunity_score` / `urgency_score` /
`priority_score` was considered and **rejected**: every weighting would be
invented here rather than governed, it would have no defensible meaning for
no-deadline or not-applicable opportunities, and a single ordered number
rendered next to opportunities reads as a recommendation — which is the
optimizer's authority, not this model's.

Ordering is presentation only. `_attention_key` is Assurance's key with **one**
leading term added (does any attention flag stand), and material impact stays
delegated to `candidate_rank`, the optimizer's own sealed verdict.

## 6. Actionability precedence

Highest first; first match wins. It reuses Assurance's `ActionStatus` values
where they still hold and adds the three states that only exist once timing and
the Journal are in scope.

| | Why it outranks the next |
|---|---|
| `BLOCKED` | a governed exclusion applies; the user cannot act at all |
| `ACTION_REPORTED` | the user says they already acted — labelling that window "expired" would imply they missed it |
| `EXPIRED` | the governed deadline has passed |
| `DECLINED` | the user declined; the opportunity may well still be available |
| `DEFERRED` | the user deferred — deliberately not expiry |
| `EVIDENCE_REQUIRED` | Assurance's own verdict |
| `DECISION_REQUIRED` | Assurance's own verdict |
| `ACTION_AVAILABLE` | nothing stands in the way |

Outranked is not overwritten: every axis still reports itself. A blocked
opportunity with a PROCEED decision shows both.

## 7. `COMPLETE` is deliberately absent

The Assurance entry omitted it for want of a definition. The Journal now
supplies user intent and user self-report — and still no proof that anything
occurred. So

```
decision = PROCEED    execution = USER_REPORTED    evidence = MISSING
```

remains a first-class, visible state. `ACTION_REPORTED` says exactly what is
known and no more, and the evidence gap keeps its own flag rather than being
silenced by the report. `USER_REPORTED` is never upgraded to verification, and
no `SYSTEM_VERIFIED` was added merely because this lifecycle would find one
useful.

## 8. The Journal join

`decision_journal.subject_opportunity_code` is **nullable with no uniqueness
constraint**, so both partial and multiple joins are real:

- A thread naming no opportunity joins to none. It is reported as
  `summary.unlinked_thread_count` rather than dropped — a thread the lifecycle
  cannot place still exists.
- Several threads may name one opportunity. The newest wins, tie-broken by id
  so the order is total and two threads created in the same instant cannot swap
  between requests. `journal.thread_count` exposes the rest: a user who decided
  twice has two threads, and showing only the latest without saying so would
  make the others look as though they never happened. The Journal remains the
  history authority for all of them.
- Silence is `NO_DECISION`, which is not `CONSIDERING`. Inferring the latter
  would put words in the user's mouth.

## 9. Persistence: none

No tables, no columns, no migrations, no cache. Migration head is unchanged.
Durable opportunity-state *transitions* are a different question with a
different owner — the later retention / "what changed?" engine — and adding
them here would have meant persistence this entry forbids.

## 10. Measured

- **Zero business authority on the read.** Engine `compute` and
  `RulesEvaluatorService.evaluate` counters are installed around the *fixture
  build first* and required to trip there, then cleared before the request — a
  counter that cannot detect real work proves nothing about its absence. The
  patch targets every module in `sys.modules` that actually holds a reference
  to `compute`, enumerated rather than listed, because patching one import site
  while a caller binds another is how a counter test goes silently blind.
- **Nothing after the last load touches the database.** Unlike the Assurance
  map, this path legitimately queries after the graph build — the Journal is
  its second source — so the measured boundary is `list_journals`: **0**
  statements after it returns.
- **The Journal load is flat in thread count.** 1 thread and 4 threads both
  cost the same total statements; a per-thread event lookup would show as a
  rising count.
- **Determinism.** Repeated requests are byte-identical; the pure derivation is
  stable across `PYTHONHASHSEED` 0/1/42.
- **Tenancy.** Two users holding the *same* opportunity code — the join key —
  see only their own threads.
