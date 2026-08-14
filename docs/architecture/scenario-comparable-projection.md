# SCENARIO-COMPARABLE PROJECTION (Entry 12B1, §17)

The read-model transformation that prepares **one** authoritative historical
graph for a single-scenario baseline-versus-counterfactual comparison, and the
table of verdicts it applies.

It is **not** the comparator. Nothing in §17 diffs two graphs, and no
`ADDED` / `REMOVED` / `CHANGED` / `UNCHANGED` vocabulary exists anywhere in it.
Comparison is Entry 12B.

```
load_sealed_sides(session, user_id, scenario_id)     §16, sealed rows only
        │
        ├── assemble_historical_graph(baseline,      pure, no session
        │       user_id=…)
        └── assemble_historical_graph(counterfactual,
                user_id=…)
                    │
                    └── project_scenario_comparable_graph(graph)
                                → ScenarioComparableGraph
```

## 1. The invariant the whole section turns on

```
zero nodes in a comparable family   !=   a family that does not apply
```

A single scenario has no portfolio, so it has no resource ledger: `RESOURCE` is
**inapplicable**, not empty. Expressing that by dropping the nodes and saying
nothing would leave a later comparator reading "baseline had N resources, the
counterfactual has none" and reporting N phantom removals.

So applicability is carried as a **value**, one entry per family, and
`FamilyApplicability.retained` distinguishes the two cases by construction:

| case | `status` | `retained` |
|---|---|---|
| comparable, this side holds three | `COMPARABLE` | `3` |
| comparable, this side holds none | `COMPARABLE` | `0` |
| does not apply to one scenario | `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` | `None` |
| meaningful, no sealed source today | `CURRENT_SOURCE_UNAVAILABLE` | `None` |
| identity deliberately not retained | `READINESS_SEMANTICS_ONLY` | `None` |
| reserved in the 12A taxonomy | `RESERVED_NO_PRODUCER` | `None` |

## 2. Node applicability

| node type | verdict | reason |
|---|---|---|
| `FACT` | `COMPARABLE` | frozen snapshot, plus this side's sealed lever changes |
| `TAX_STATE` | `COMPARABLE` | sealed result header and sealed line items |
| `OPPORTUNITY` | `COMPARABLE` | sealed normalized candidates |
| `DEADLINE` | `COMPARABLE` | reached through each candidate's **pinned** rule version |
| `EVIDENCE` | `COMPARABLE` | sealed requirements + sealed held **type** semantics + derived readiness |
| `RESOURCE` | `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` | a single scenario has no portfolio ledger |
| `ASSUMPTION` | `COMPARABLE` | sealed rows |
| `SCENARIO` | `COMPARABLE` | sealed row |
| `OBLIGATION`, `DECISION` | `RESERVED_NO_PRODUCER` | reserved in 12A, zero producers |

## 3. Edge applicability

| edge | verdict | reason |
|---|---|---|
| `DERIVED_FROM` | `COMPARABLE` | line item → sealed result header |
| `REQUIRES` | `COMPARABLE` | opportunity → evidence requirement |
| `EXPIRES_AT` | `COMPARABLE` | opportunity → deadline |
| `ASSUMES` | `COMPARABLE` | scenario → assumption |
| `REFERENCES_SCENARIO` | `COMPARABLE` | scenario → tax state |
| `SUPPORTED_BY` | `READINESS_SEMANTICS_ONLY` | §5 below |
| `INELIGIBLE_BECAUSE` | `CURRENT_SOURCE_UNAVAILABLE` | §4 below |
| `CONSTRAINED_BY` | `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` | portfolio ledger |
| `CONSUMES_RESOURCE` | `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` | portfolio ledger |
| `CONFLICTS_WITH` | `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` | run-scoped pairwise portfolio relationship |
| `REFERENCES_RULE` | `RESERVED_NO_PRODUCER` | no live `RULE` node type |

## 4. `INELIGIBLE_BECAUSE` is unavailable, not permanently non-comparable

Measured, not assumed: the edge has **exactly one** producer today —
`GraphAssembler._edges_from_portfolio`, reading `self.src.exclusions`
(`ioe.portfolio_exclusion`).

Its semantics are perfectly meaningful for a single action: "this was blocked by
that". What is missing is a **sealed single-scenario source**, because the only
producer that exists is portfolio-scoped.

Recording that as `NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON` would encode a
permanent blanket exclusion that nobody decided, and would silently absorb a
future scenario-level producer into a verdict reached about a different one. So
it is `CURRENT_SOURCE_UNAVAILABLE`, and
`tests/unit/state_graph/test_projection_producer_coverage.py` asserts both the
producer count **and** that the producer still reads the portfolio exclusion
rows the classification rests on.

Today: no applicable source → no historical `INELIGIBLE_BECAUSE` edge.
A future scenario-level producer → the guard fails until policy is reviewed.

## 5. `SUPPORTED_BY` keeps readiness and invents no identity

The live 12A edge terminates on `docs.document` — document **identity**.
Historical Held-Evidence Semantics deliberately chose `READINESS_SEMANTICS_ONLY`,
so a sealed bundle carries governed `ref.document_type.code` values and no
document ids, ever.

Preserved: required evidence, sealed held-evidence **type** semantics,
deterministic readiness through the same `readiness_for` the live graph uses,
and therefore a fully observable `READY → MISSING` transition — which is what
makes the evidence axis worth comparing at all.

Never fabricated: document ids, fake historical documents, or synthetic
historical `SUPPORTED_BY` identity edges. The projection excludes the
`EVIDENCE:docs.document` sub-kind **structurally**, so the guarantee holds for
any input rather than only for inputs that happen to lack identity today.

## 6. Symmetry

One implementation, called twice:

```python
baseline_projected       = project_scenario_comparable_graph(baseline_graph)
counterfactual_projected = project_scenario_comparable_graph(counterfactual_graph)
```

The function receives no side marker and has no branch on one, so a
baseline-only or counterfactual-only filter is unreachable rather than merely
absent. The behavioural proof compares the two sides' **verdicts** family by
family, not just that the same function was called twice: a policy difference
would surface as a difference in applicability rather than in content.

## 7. Determinism, idempotence, purity

* **Deterministic** — invariant under node and edge insertion order, dict and
  source ordering, and `PYTHONHASHSEED` 0 / 1 / 42 in real subprocesses. The
  canonical representation is built on `graph_hash_payload` and
  `canonical.canonical_text`; no second serializer and no new hash domain, since
  a projection is a derivable view of an already-hashed graph rather than a
  sealed artifact.
* **Idempotent** — `ScenarioComparableGraph.graph` is a real `TaxStateGraph`, so
  `project(project(g).graph) == project(g)` holds literally. A second pass can
  never quietly remove more.
* **Pure** — zero database queries, zero `TaxEngineService`, zero
  `RulesEvaluatorService`, zero optimizer, zero latest-rule resolution, zero
  `docs.document`, zero persistence. Proved by counters over the complete
  customer historical read, not by inspection.

Closure is enforced rather than patched: a retained edge whose endpoint this
projection removed raises `ScenarioProjectionError`, because that means the node
and edge tables contradict each other. An edge already dangling on the way in is
carried through — the projection is not a validator of its input.

## 8. Storage

Nothing. No table, no column, no migration, no persisted graph.

A projected historical graph is a deterministic function of artifacts that
cannot change, so storing it would store a derivable value. Privacy universe
stays at **70** and the Alembic head stays at **0067_counterfactual_state**.

## 9. Known limitation, carried forward to Entry 12B

`load_sealed_sides` builds the baseline side with **no** sealed line items and
**no** sealed candidates:

```python
baseline = _side(SIDE_BASELINE, (), (), ())
```

Measured on a real sealed v2 scenario, the two projected sides come out as:

| family | baseline | counterfactual |
|---|---|---|
| `FACT` | 27 | 27 |
| `TAX_STATE` | 1 (header only) | 7 (header + 6 sealed line items) |
| `OPPORTUNITY` | 0 | 1 |
| `DEADLINE` | 1 | 1 |
| `EVIDENCE` | 2 | 2 |
| `RESOURCE` | inapplicable | inapplicable |
| edges | 1 | 9 |

Both baseline families project as **`COMPARABLE` with `retained == 0`**, which is
exactly the state §1's invariant exists to keep distinct from inapplicability —
so §17 reports it correctly. But a comparator run against these two sides today
would read all six counterfactual line items and the one counterfactual
opportunity as **additions**, because the baseline bundle carries nothing to
match them against.

That is a **§16 bundle-content gap, not a projection defect**: the baseline's tax
state lives in `analysis.analysis_run` / `analysis.analysis_line_item`, which the
sealed-source loader does not read. Resolving it means teaching
`load_sealed_sides` to load the baseline's sealed analysis detail — sealed rows,
no recomputation, no live fallback — and it must be done before the Before-You-Act
comparison presents differences to a user.
