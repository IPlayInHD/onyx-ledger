# BEFORE-YOU-ACT COMPARISON API (Entry 12B)

The product-facing read contract over the certified comparison engine.

```
GET /api/v1/ioe/scenarios/{scenario_id}/comparison[?include_unchanged=]
```

The engine was certified separately
(`docs/architecture/before-you-act-comparison-engine.md`). This entry adds a
request path and a product contract; it adds no comparison semantics.

## 1. The path

```
authenticated request                     current_user_id + db_authed
        │                                 (db_authed applies the account-deletion cutoff)
        ├── ownership check               IoeReadRepository.get_scenario  → 404
        ├── sealed source load  §16       load_sealed_sides               ─┐
        ├── historical graph              assemble_historical_graph        │ pure
        ├── comparable projection §17     project_scenario_comparable_graph│ from
        ├── authority gate + compare      compare_scenario_graphs          │ here
        └── product serializer            before_you_act_presentation     ─┘
```

`BeforeYouActService` orders those steps and translates one exception. It is
**not** `comparison_service.py`, which compares two *different* scenarios; this
compares the two sides of *one*.

**Ownership is checked first, on its own.** `load_sealed_sides` would also refuse
someone else's scenario, but it refuses with `SEALED_EVIDENCE_INCOMPLETE` — so a
prober would see 409 for an id that exists and 404 for one that does not.
Checking ownership first keeps both inaccessible cases identical.

## 2. Authority boundary — measured, not asserted

Zero `TaxEngineService`, zero `RulesEvaluatorService`, zero optimizer, zero
current-rule resolution, zero `docs.document` reads, zero LLM. Counted through
the real HTTP path on both the success and the refusal branch, with the counters
proved non-vacuous by a real comparison coming back.

| | measured |
|---|---|
| total SQL statements | 11 |
| through the sealed source load | 11 |
| **after the source load** | **0** |
| records rendered | 96 |

Projection, comparison and serialization issue no statement at all. The
measurement instruments `load_sealed_sides` itself, so the boundary is observed
rather than assumed.

## 3. Failing closed

A scenario that cannot be compared returns an **error**, never a 200 with empty
arrays — an empty page reads as "nothing would change", which is a claim the
seal does not support.

| condition | response |
|---|---|
| not yours / does not exist | `404` — byte-identical for both |
| account past its deletion cutoff | `403` (inherited from `db_authed`) |
| seal cannot answer for a required family | `409` + `error_code` |

The 409 is `ComparisonUnavailable`, an `application/problem+json` body carrying a
closed `error_code` from `IntegrityReason` and nothing else — no message, no row,
no path, no stack.

**Both governed refusals are translated, and there are two.** The source layer
refuses what it cannot load; the engine's authority gate refuses what loaded but
cannot answer. A sealed **v2** scenario takes the *second* path — v2 is
derived-state-bearing, so its sides load cleanly and only the missing baseline
`OPPORTUNITY` authority stops it. Wrapping only the load let that case reach the
framework as a 500 with a stack trace; the acceptance suite caught it.

## 4. The product contract

`schema_version` is **this contract's** version
(`BEFORE_YOU_ACT_SCHEMA_VERSION`), deliberately independent of the scenario-result
protocol version. A client renders this payload; coupling the two would
re-version every client for a change no client can see. Both appear in the
response so a caller can tell them apart.

Change records are grouped by family — `tax_state_changes`,
`opportunity_changes`, `evidence_changes`, `deadline_changes`, `fact_changes`,
`assumption_changes`, `scenario_changes` — rather than returned as one list, so
clients do not each invent their own classification.

`family_applicability` travels with them. Without it, "no resources shown" and
"resources are not a single-scenario concept" look identical.
`RESOURCE` reports status `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` with
reason `SINGLE_SCENARIO_HAS_NO_PORTFOLIO_LEDGER`, and produces no records.

**Money and rates are strings**, exactly as the canonicalizer rendered them at
seal time. Nothing in the serializer parses a numeric field; a float is not a
money type and converting one would change the value the seal recorded.

**No strategy claims.** No savings, no ranking, no "best", no recommendation, no
probability of acceptance. This surface reports what changed. A test greps the
rendered payload for that vocabulary.

**No storage identity.** Evidence stays `READINESS_SEMANTICS_ONLY`: no document
ids, buckets, object keys or content hashes, asserted against the real values
from the fixture's own document. `comparison_hash` *is* exposed — a governed
artifact identity in the same family as the already-public
`scenario_result_hash`, not a storage identifier.

## 5. The UNCHANGED decision

**Default: changes only.** `?include_unchanged=true` renders the rest.

The customer question is "what would change if I did this", and an unchanged
line answers it with noise — the full comparison here rendered 96 records where
the changed subset is a small fraction.

**The filtering is presentation only**, and two properties make that safe rather
than merely stated:

- `comparison_hash` is the engine's hash of the **full** comparison.
- `summary` counts come from the **full** comparison, not from the rendered lists.

So a client cannot change what the comparison *says* by changing what it *asks
for*. Asserted directly: both responses carry the same hash and the same summary,
and the lean response's keys are a subset of the full one's.

## 6. Determinism

Three identical requests return identical bodies. The correlation id lives in
the log line and the error envelope, never in the artifact. Ordering comes from
the engine's own sorted records — the serializer re-sorts nothing, because a
second ordering rule would be a second answer to what "first" means.

Seal, read, then churn current financials, publish a new rule and hold a new
document: the payload and hash are unchanged, with the engine, rules evaluator
and `docs.document` all proved untouched.

## 7. Persistence

None. No table, no column, no migration, no cache. A comparison is a pure
function of two sealed artifacts that cannot change, so storing it would store a
derivable value. The measured post-source-load cost gives no reason to revisit
that.
