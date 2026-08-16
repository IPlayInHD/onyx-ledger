# TAX ASSURANCE MAP (Entry: Tax Assurance Map / Product Readiness Model)

The deterministic product-readiness read model over the CURRENT-view Personal
Tax State Graph, and the API that serves it.

```
GET /api/v1/ioe/assurance?tax_year=YYYY[&as_of=YYYY-MM-DD]
```

```
GraphLoader                       the ONLY live reads (certified, Entry 12A)
    └── TaxStateGraphService.build(tax_year)  → TaxStateGraph
            └── derive_assurance_map(graph, as_of=…)   pure, no I/O, no clock
                    └── assurance_detail(…)            pure serializer
                            └── TaxAssuranceOut        schema v1.0.0
```

## 1. What it is, and is not

It answers, from governed structured state: which opportunities exist, what
stands between the user and acting on each (evidence, a decision, a governed
exclusion), what is urgent, which parts of the position are ready, unavailable
or not applicable, and what to review next in a documented order.

It computes no tax, decides no eligibility, resolves no rule, reads no
document content and ranks nothing the optimizer has not already ranked. Every
figure was produced by the authority that owns it; the model's entire
contribution is a **closed status vocabulary** and the **documented precedence**
between conditions that already exist.

## 2. Mode: current-state only (decision A)

The graph's CURRENT view is the one certified read model of live governed
state. Historical assurance for a sealed scenario is a different question with
a different authority — already answered by the Before-You-Act comparison —
and blending the two would mix live opportunity state with frozen evidence.

**Live authorities deliberately allowed in current mode:** exactly the
`GraphLoader`'s reads (analysis rows, sealed candidates, pinned rule
requirements and deadlines, held-document *types*, scenarios, resource
ledger, profile/financial rows). **Deliberately not allowed and measured at
zero:** `TaxEngineService`, `RulesEvaluatorService`, optimizer execution,
latest/current rule resolution, `docs.document` content. A rule published
after the run does not change the map: candidates are sealed, and
`test_a_rule_published_after_the_run_changes_nothing` pins that.

## 3. The status model

`AssuranceStatus` — items and families alike, precedence highest first:

```
UNAVAILABLE > BLOCKED > REVIEW_REQUIRED > EVIDENCE_REQUIRED > READY
```

plus `NOT_APPLICABLE` for the two reserved taxonomy families. Each value has a
single derivation rule:

| status | fires when |
|---|---|
| `BLOCKED` | a governed exclusion exists (`exclusion_reason_code`, or an `INELIGIBLE_BECAUSE` / `CONSTRAINED_BY` edge) |
| `REVIEW_REQUIRED` | `requires_re_evaluation`, stale freshness, or `indeterminate` eligibility — each named by a closed reason code |
| `EVIDENCE_REQUIRED` | readiness `MISSING`, `PARTIAL`, or `UNKNOWN` — unknown is a gap, not a pass |
| `READY` | none of the above |
| `UNAVAILABLE` | family-level: the governing authority is absent |

**UNAVAILABLE never becomes READY-empty.** A family with no optimization run
reports `UNAVAILABLE / NO_OPTIMIZATION_RUN_FOR_TAX_YEAR`; a run with zero
candidates reports `READY` with zero items. Zero records under absent
authority and zero records under present authority are different claims, and
the family row is what keeps them different.

**ASSUMPTION fails closed.** A run with zero assumption nodes is either a
pre-authority seal or a genuinely empty declaration, and the graph does not
record which — so the family reads `UNAVAILABLE / ASSUMPTION_DECLARATION_ABSENT`
rather than claiming an authoritative emptiness nobody recorded.

`ActionStatus` — a deterministic projection of (status, eligibility):
`ACTION_AVAILABLE`, `DECISION_REQUIRED` (review owed, or `conditionally_eligible`
while ready), `EVIDENCE_REQUIRED`, `BLOCKED`. **`COMPLETE` is deliberately
absent**: nothing in governed state records that a tax action was actually
taken — a simulated scenario is not a taken action. It awaits the Decision
Journal authority.

`UrgencyStatus` — its own axis, never folded into standing:
`NO_DEADLINE / NORMAL / APPROACHING / URGENT / EXPIRED`, with central
thresholds (`URGENT_WITHIN_DAYS = 14`, `APPROACHING_WITHIN_DAYS = 60`) defined
once in `assurance.py`. The earliest governed deadline binds; all are counted;
none is ever fabricated.

## 4. Time

`as_of` is an explicit, injectable input — a query parameter defaulting to
today (UTC) at the route boundary — echoed in the response, and it touches
**only** deadline urgency. Same graph + same `as_of` ⇒ same map, proved under
`PYTHONHASHSEED` 0/1/42 and across repeated HTTP requests.

## 5. The attention queue

`attention` orders item ids by a single documented key:

1. urgency band (urgent → approaching → normal → no deadline → **expired
   last**, because the window has closed);
2. action band (closable gaps first: evidence, then decisions, then ready,
   blocked last);
3. **the optimizer's own sealed `candidate_rank`** — material-impact ordering
   is delegated to the authority that already ranked it, which is what keeps
   this a presentation order rather than a second recommendation engine;
4. identity, so the order is total.

No optimality is claimed. There is no overlap with portfolio optimization
authority: this module never re-ranks by amount.

## 6. Deliberately not implemented

- **A numeric "assurance score."** No governed model defines what such a
  number measures; an unweighted blend of readiness, urgency and support
  would invite reading it as a probability of correctness. The summary stays
  multi-dimensional counts.
- **"What changed since last visit."** There is no governed
  previous-current-state authority to compare against. Recorded as a future
  retention-engine dependency; not manufactured. (Scenario-vs-baseline change
  is already served by the Before-You-Act API.)

## 7. Support, evidence, resources

Support travels as the existing `SupportScore` type, so the governed
disclaimer accompanies every score; `assumption_dependent` is read off the
support model's own output (adjusted ≠ raw), never re-derived. Evidence
identity is the document **type** — no document ids, buckets, object keys or
content hashes, asserted against the fixture's own real values. `RESOURCE`
values pass through the governed ledger verbatim; when no run exists the
family is `UNAVAILABLE`, never zero capacity.

## 8. Freshness vs integrity

Carried per item as four separate fields — `freshness` + `stale_reason_codes`,
`integrity` + `integrity_reason_code` — exactly as the graph reports them.
Never collapsed into a generic "outdated".

## 9. Measured

| | |
|---|---|
| SQL per request | 22 total, all in the graph load, **0 after the build** |
| derivation + serialization | ~49–52 µs/item, flat from 200 → 2000 items |
| payload | ~3 KB (1 item) to ~1.6 MB (2000-item stress synthetic) |
| business authorities on a read | engine 0 · rules 0 (counters proved non-vacuous) |

## 10. Persistence

None. No table, no column, no migration, no cache. The map is a pure function
of a graph that already proves its own identity (`graph_hash` is echoed as
the provenance anchor), so persisting it would persist a derivable value.
