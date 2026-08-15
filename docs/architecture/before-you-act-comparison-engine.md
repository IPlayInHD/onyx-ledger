# BEFORE-YOU-ACT COMPARISON ENGINE (Entry 12B)

The deterministic difference between a **frozen baseline** and a **sealed
counterfactual**, expressed over the Personal Tax State Graph.

It is the comparator §17 deliberately did not build. §17 prepared one side;
this consumes two.

```
load_sealed_sides(session, user_id, scenario_id)      §16, sealed rows only
        │                                             ── the only I/O ──
        ├── assemble_historical_graph(baseline)       pure
        └── assemble_historical_graph(counterfactual)
                    │
                    └── project_scenario_comparable_graph(…)   §17, pure
                            │
                            └── compare_scenario_graphs(
                                    ComparisonSide(baseline, authority),
                                    ComparisonSide(counterfactual, authority))
                                        → TaxStateComparison
```

**Engine only.** No API route, no persisted row, no cache, no frontend. A
comparison is a pure function of two artifacts that can never change, so storing
it would store a derivable value; that is a product decision for the next entry,
not a consequence of this one.

## 1. What it is not

It computes no tax, decides no eligibility, resolves no rule, reads no document
and issues no query. Every value it reports was already determined by the
authority that owns it, and every value it subtracts was already sealed.

Subtracting two sealed tax figures is a comparison. It is not a second tax
engine — which is the whole reason this module is allowed to live beside
`TaxEngineService` rather than inside it. Two integration tests hold the line
by counting: `TaxEngineService.compute` and `RulesEvaluatorService.evaluate` are
wrapped for the duration of a comparison and must record **zero** invocations,
and a SQLAlchemy `before_cursor_execute` listener must observe **zero**
statements after the sources are loaded.

**It describes differences. It does not recommend.** There is no "savings", no
"best", no "optimal" and no ranking. Those are strategy determinations owned
elsewhere, and a comparator that produced one would be deciding what a person
should do while claiming only to describe what changed. A test greps the module
for that vocabulary.

## 2. The gate runs first

```python
assert_authority_complete(baseline.authority)
assert_authority_complete(counterfactual.authority)
```

A side reporting `MISSING_AUTHORITY` for any comparison-required family is
**refused**, through the source layer's own `SEALED_EVIDENCE_INCOMPLETE`
verdict — not a partial comparison, not an empty one.

This is not defensive coding. A comparator handed a family that was never loaded
cannot distinguish it from one that is genuinely empty, and would report every
counterpart on the other side as something the scenario *caused*. Every sealed
**v2** scenario is exactly that case: it carries no baseline opportunity
authority, so `test_a_legacy_v2_scenario_is_refused_rather_than_compared` pins
the refusal — and asserts the refusal path evaluates no rules on its way out.

`ComparisonSide` pairs a projected graph with its authority map because neither
is sufficient alone: the projection says what a side **contains**, the authority
says whether the source was ever **consulted**.

## 3. Matching is by certified identity only

| | matched on |
|---|---|
| nodes | `GraphNode.key` |
| edges | `GraphEdge.sort_key` — type plus both endpoints |

No fuzzy matching, no label matching, no positional matching, no regenerated
identity. Field comparison reuses `hashing.node_payload` / `edge_payload` — the
same canonical payloads the graph hash is built from — so the comparator cannot
drift into a second notion of equality. Making those two functions public was
the alternative to copying them.

One semantic identity resolving to two different families raises
`ComparisonIntegrityError` rather than reporting `CHANGED`: that is a broken read
model, and rendering it as a change would present it as a movement in the user's
tax position.

## 4. The four verdicts

| | |
|---|---|
| `ADDED` | on the counterfactual, absent from the baseline |
| `REMOVED` | on the baseline, absent from the counterfactual |
| `CHANGED` | matched, at least one non-identity field differs |
| `UNCHANGED` | matched, every field agrees |

Identity fields (`key`, `node_type`, `source_kind`, `source_id`) are excluded
from field comparison: a matched pair agrees on them *by construction*, and
emitting them would pad every record with four values that can never differ.

**`CHANGED` is rare for edges, and that is correct.** No edge family the
projection retains carries an attribute, so a semantic change to one is a
*different edge* — `REMOVED` + `ADDED` is the honest rendering. The attribute
comparison is kept anyway so a family that later acquires a payload under a
stable identity is compared rather than silently reported unchanged;
`test_no_retained_edge_family_can_report_changed_today` pins today's reality so
the branch cannot rot unnoticed.

## 5. Deltas are governed, not arithmetic

A `FieldChange.delta` is emitted **only** for an attribute in
`_MONEY_ATTRIBUTES` or `_RATE_ATTRIBUTES`, rendered back through
`canonical.money` / `canonical.rate` — the same canonicalizer the values came
from, so a delta is expressed in the scale of the thing it measures.

No delta is produced when either side is `None`. **Absent is not zero**, and a
delta against an absent value would state a movement of a size the seal never
recorded. A non-numeric stored value is a malformed artifact, not a difference:
`before` and `after` are still reported, the arithmetic is not.

## 6. Applicability survives the comparison

The `FamilyApplicability` triples from §17 are carried through, with `retained`
dropped to `None`.

`retained` is how many nodes **one side** holds — the very thing a comparison
measures — so quoting one side's count would silently present the baseline's
holdings as the comparison's. The distinction §17 exists to preserve survives
regardless: a family that is comparable and empty reads `COMPARABLE` with zeros
in `summary.family_counts`; a family that does not apply says so in its status.

The two sides must agree on the applicability **policy** (family, status, reason)
or the comparison is refused. They are free to disagree on `retained`. A
disagreement about policy means the sides came from different projection rules,
and comparing them would attribute that to the user.

## 7. Direction is part of the contract

`BASELINE_TO_COUNTERFACTUAL` — "what would change if you did this", never the
reverse.

`invert(compare(A, B)) == compare(B, A)` is asserted, over synthetic sides and
over real sealed state. Inversion swaps `ADDED`/`REMOVED`, swaps `before`/`after`
and negates every delta. A comparator that is not invertible is one whose
direction is an accident rather than a decision.

`compare(A, A)` reports `UNCHANGED` throughout and an empty `changed_families`.

## 8. Hash and determinism

`comparison_hash` is domain-separated under `DOMAIN_SCENARIO_COMPARISON`
through the existing `canonical.domain_hash`, which refuses an unregistered
domain — that is what makes reuse enforceable rather than conventional.

The payload binds **both** graph hashes. A comparison is only meaningful about
the two artifacts it was taken over, and a digest that did not name them could
be presented beside a different pair.

`canonical_comparison_payload` is public so a test can assert what enters the
hash rather than infer it from a digest. Determinism is proved by re-running the
comparison in subprocesses under `PYTHONHASHSEED` 0, 1 and 42, and by asserting
invariance under input insertion order.

## 9. Cost

Keyed maps by semantic identity, never pairwise matching — `O(n + m)`. Measured
by `test_comparison_cost_is_measured_and_linear`:

| | elements | comparison | per element | output |
|---|---|---|---|---|
| small | 42 | 1.0 ms | 24.7 µs | 8 KB |
| moderate | 802 | 10.5 ms | 13.0 µs | 77 KB |
| stress | 8 002 | 103.5 ms | 12.9 µs | 736 KB |

Per-element cost is **flat** from moderate to stress; `small` is higher only
because fixed setup is amortized over 42 elements. A pairwise matcher would show
the opposite shape.

## 10. No live fallback

Seal, compare, then churn current financials, publish a new rule and hold new
documents. The comparison is **byte-identical**: same canonical text, same hash,
same records — with the engine, the rules evaluator and `docs.document` all
proved untouched.

A comparison is a statement about two sealed artifacts. If current state could
move it, it would be a statement about now, and the seal would be decoration.

## 11. Test inventory

| file | tests | layer |
|---|---|---|
| `tests/unit/ioe/test_scenario_comparison.py` | 31 | pure domain |
| `tests/integration/test_scenario_comparison_engine.py` | 10 | end-to-end, real seals |

Every integration test establishes its own governing rule state; none relies on
shared-database residue. Proved green alone on a fresh database, in combination,
and in reversed order.

## NEXT

Entry 12B — comparison API and product contract. The engine deliberately stops
at the function boundary.
