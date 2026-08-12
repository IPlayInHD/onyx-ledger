# BEFORE-YOU-ACT SIMULATION — architecture, and the contract that is missing

**Status: BLOCKED at the counterfactual-graph boundary.** This document is the
§37 stop: the scenario engine yields enough governed output to reconstruct a
counterfactual *input* state, and not enough to construct a counterfactual
*derived* state. The difference decides whether the headline product output —
"which opportunities change if I do this" — can be produced authoritatively.

It cannot, today. What is missing is named precisely in §6 so a later entry can
add it deliberately rather than discover it.

Nothing was built on top of the gap. No invented counterfactual facts, no
comparison-time eligibility evaluation, no new tables, no migration.

## 1. What the product needs

> If the user takes a permitted action, how would their governed tax state
> differ from the current baseline?

Answering that requires two governed states and a deterministic difference
between them. The states are the hard part; the difference is not.

## 2. Authorities, unchanged

| concern | authority | 12B may |
|---|---|---|
| tax calculation | `TaxEngineService` | read sealed results |
| eligibility | `RulesEvaluatorService` | read sealed verdicts |
| counterfactual execution | `ScenarioService` | read sealed scenarios |
| portfolio / resources | optimization services | read sealed ledgers |

A comparison layer **may describe differences; it must not create them**. That
single rule is what this document ends up enforcing, at the cost of the entry.

## 3. What the scenario engine actually seals

Read from `app/services/ioe/scenario/service.py::_persist`, which is the only
writer. One completed scenario produces exactly:

```
ioe.scenario                    header, hashes, pins, freshness, integrity
ioe.scenario_result             baseline_tax, scenario_tax, tax_delta,
                                objective values, five support scores,
                                affected_rule_versions
ioe.scenario_confidence_component   per-factor support breakdown
ioe.scenario_input_change       lever_code, field, old_value, new_value, order
ioe.scenario_lever              the typed specification
ioe.scenario_assumption         typed assumptions
ioe.run_rule_snapshot           the rule pin
```

And `_compute` shows why that list is what it is:

```python
clone = frozen.baseline_clone()
applied = lever_registry.apply_all(clone, applications, ...)
scenario_result = compute(to_tax_input(applied.inputs))
```

The counterfactual inputs (`applied.inputs`) exist **in memory for the duration
of one call** and are never persisted. Only `applied.changes` — the field-level
old→new trace — survives. The service says so itself:

> The applied-change trace records FIELD NAMES and the values the registry
> produced. It does not copy the user's other financial inputs; those stay in
> the frozen analysis snapshot.

That is a good decision. It is also the one that shapes everything below.

## 4. What IS authoritatively reconstructable

**The counterfactual input state.** `Scenario.base_analysis_id` names the
baseline; `analysis.analysis_input_snapshot` holds it frozen with a
`snapshot_hash`; `FrozenAnalysisInput` already reconstructs it; and
`ioe.scenario_input_change` records every field the levers moved. Baseline plus
changes is the counterfactual input, derived entirely from persisted, hashed,
governed rows. No live read, no invention.

So these comparisons are available today:

```
tax delta            ScenarioResult.baseline_tax / scenario_tax / tax_delta
input changes        scenario_input_change, field-level, registry-produced
assumptions          scenario_assumption, typed and registered
support scores       the five fields, before and after
objective            objective_value_baseline / _scenario / _delta
freshness            scenario.freshness_status + stale_reason_code
integrity            scenario.integrity_status + integrity_reason_code
supersession         superseded_by_scenario_id / refreshed_from_scenario_id
rule pin             affected_rule_versions, rule_snapshot_id
```

That is a real and useful comparison. It is not a State Graph comparison.

## 5. What is NOT reconstructable — the blocker

A scenario **never evaluates eligibility**. Confirmed three ways: the service
imports no rules evaluator; `_persist` writes no candidate rows; and no table in
`ioe` carries both `scenario_id` and a candidate, portfolio or ledger reference.

The consequences, mapped onto the eight live 12A node types:

| 12A node type | counterfactual source | verdict |
|---|---|---|
| `FACT` | frozen snapshot + `scenario_input_change` | **available** |
| `TAX_STATE` | `scenario_result` totals | **header only** — no counterfactual `analysis_line_item` exists |
| `ASSUMPTION` | `scenario_assumption` | **available** |
| `SCENARIO` | `ioe.scenario` | **available** |
| `EVIDENCE` | unchanged by a scenario, by design (§21) | **available, always unchanged** |
| `DEADLINE` | pinned rule versions, unchanged within a scenario | **available, always unchanged** |
| `OPPORTUNITY` | — | **MISSING** |
| `RESOURCE` | — | **MISSING** |

`OPPORTUNITY` is the one that matters. §20 calls opportunity comparison "a major
user-facing output", and the transitions the product wants —
`NEWLY_AVAILABLE`, `NO_LONGER_AVAILABLE`, `IMPACT_CHANGED`,
`CONSTRAINT_CHANGED` — are all statements about eligibility under the
counterfactual inputs. Nothing has ever computed that.

### Three ways to fake it, and why each was refused

1. **Evaluate eligibility at comparison time.** This is the tempting one,
   because `RulesEvaluatorService` is right there and would be *the* authority.
   Refused: it makes the read path create the difference rather than describe
   it, produces a verdict that is part of no sealed artifact, and would drift
   whenever rules change — so two reads of the same sealed scenario could
   disagree. §35 forbids the comparator calling the evaluator for exactly this
   reason.
2. **Infer opportunity changes from the tax delta.** Refused: that is inventing
   tax law. A tax delta says nothing about which rule became satisfiable.
3. **Reuse the baseline's opportunities as the counterfactual's.** Refused: it
   would report "no opportunity changed" for every simulation, which is a
   confident false answer rather than an absent one.

`RESOURCE` fails the same way one level down: the ledger belongs to portfolio
assembly, and no portfolio is assembled for a scenario.

## 6. The missing contract, stated precisely

For Before-You-Act to produce authoritative opportunity changes, the scenario
engine must seal a counterfactual eligibility evaluation. Concretely, one
completed scenario would additionally need to persist, inside its existing TX-2
and under its existing `scenario_result_hash`:

```
counterfactual candidate set
  per candidate: opportunity_code, tax_rule_version_id,
                 eligibility_status, calculation_basis, evidence_status,
                 standalone_potential, the five support scores
  evaluated by RulesEvaluatorService against `applied.inputs`
  against the SAME pinned rule snapshot the scenario already holds
```

Three properties make this the right shape rather than a convenient one:

* it runs **inside the scenario's own evaluation**, where `applied.inputs`
  already exists in memory — so nothing has to be reconstructed and no second
  execution path appears;
* it is pinned to the rule snapshot the scenario **already** pins, so the
  verdict is reproducible for as long as the scenario is;
* it enters the sealed result hash, so a comparison reads a fact rather than
  recomputing one.

Resources would follow the same shape if the product needs them, and are a
larger question because they belong to portfolio assembly rather than to
single-scenario evaluation.

**This is scenario-engine work, not comparison-layer work.** That is the whole
reason this entry stopped: building it inside 12B would have created a second
counterfactual execution authority, which §3 forbids in as many words.

## 7. What a later entry should build, in order

1. **Seal counterfactual eligibility in `ScenarioService`** (§6). Bounded, sits
   in an existing transaction, needs one new child table of `ioe.scenario`
   which brings its own privacy-registry, purge-keyhole and write-cutoff work —
   the 71st table, priced now rather than discovered.
2. **Then** the counterfactual source bundle, via the 12A `GraphSources`
   contract. 12A deliberately shipped `GraphView.CURRENT` only and left
   `HISTORICAL` declared; a scenario bundle is the same abstraction, populated
   from frozen artifacts rather than live rows. **One assembler, two bundles** —
   never `BaselineGraphAssembler` / `ScenarioGraphAssembler`.
3. **Then** the pure comparator: `compare_tax_state_graphs(baseline,
   counterfactual, scenario_metadata) -> TaxStateComparison`, with no session,
   no engine calls, matched on the 12A stable `node_key`, an `O(N + E)` diff
   over dicts, and a `DOMAIN_TAX_STATE_COMPARISON` hash registered in the
   existing `ALL_HASH_DOMAINS`.

Steps 2 and 3 are straightforward once step 1 exists. They are not
straightforward before it, and no amount of comparison-layer cleverness
substitutes for the sealed evaluation.

## 8. Decisions this entry did make, and that should survive

**Baseline authority.** The comparison baseline is the analysis named by
`scenario.base_analysis_id`, verified against
`scenario.baseline_input_snapshot_hash` — never live financial or profile rows.
A sealed scenario compared against a moving baseline would report differences
the user never caused. This is load-bearing and must be tested destructively
when the layer is built.

**No live fallback.** If a scenario was sealed against baseline A and the
account's facts are now B, the historical comparison stays on A. The only way to
see B is to run a new scenario.

**Reserved types stay reserved.** A comparison cannot manufacture node types
that have no producer. `OBLIGATION` and `DECISION` remain at zero producers, and
`REFERENCES_RULE` remains a reserved edge with rule identity carried as a
`tax_rule_version_id` attribute. A comparison layer is exactly the kind of
place where a synthetic "you now have an obligation" node would look reasonable
and be unsupported.

**Direction semantics.** `ScenarioResult.tax_delta` is already
`baseline_tax − scenario_tax`, so **positive means less tax**. Any comparison
contract must reuse that convention rather than introduce a second one, and must
not relabel it "savings" — some simulations model additional income, higher tax
payable, or a liquidity cost. Neutral names (`tax_change`, `cash_requirement`,
`resource_change`) first; presentation later.

**Freshness and integrity stay separate.** A simulation can be
`integrity = verified` and `freshness = stale` at once, and that combination is
valid and must be reportable. 12A already keeps these axes apart.

**Version coherence already exists.** `assert_comparable()` in
`domain/freshness.py` refuses pairs differing in baseline, year, jurisdiction,
objective policy or result schema. `ScenarioComparisonService` already applies
it for scenario-vs-scenario comparison. A baseline-vs-counterfactual layer
should reuse it, not restate it.

**No new persistence for the comparison itself.** The comparison is derivable
from sealed artifacts; storing it would store a derivable value and add a
personal-data surface to a privacy universe certified at 70 tables with zero
engineering blockers.

## 9. What already exists and must not be duplicated

`app/services/ioe/scenario/comparison_service.py` already compares **two
scenarios**, with ownership checks, sealed-and-completed preconditions,
`assert_comparable()`, and typed deltas deliberately kept apart by concept
(objective, tax, refund/balance, liquidity, per-effect-type). Before-You-Act is
a different comparison — **baseline versus counterfactual** rather than
scenario versus scenario — but it should extend that vocabulary rather than
invent a parallel one.

## 10. Privacy and lifecycle

Unchanged, and no new surface. A comparison would read only artifacts the
tenant already owns, through the ordinary protected-route admission model, with
no graph- or simulation-specific exemption around `db_authed`. An account past
its deletion cutoff is refused by the existing check; after terminal removal the
scenario rows are gone and a comparison is simply not found. Nothing here
reconstructs a deleted person's Decision Twin from retained evidence.
