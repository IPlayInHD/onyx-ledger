# COUNTERFACTUAL STATE PRODUCER PARITY (Entry 12B1, §3)

The matrix that must exist before any schema or production code changes. One row
per live State Graph node type, plus the edge families, answering the only
question that matters: **can a counterfactual node mean the same thing as its
baseline counterpart, from authoritative sealed evidence, with no live
fallback?**

Three findings changed the shape of this entry before a line was written. Two
make it much smaller than expected. One makes it harder.

## 0. The three findings

**Finding 1 — the eligibility capability already exists, unused.**
`RulesEvaluatorService.evaluate()` already takes `pinned_rule_version_ids` and
already documents the exact semantics §7 requires:

> `pinned_rule_version_ids` CONSTRAINS the evaluation to an exact, immutable
> version set. When supplied, only those versions are considered — a rule
> published after the set was pinned cannot enter the result… Passing an empty
> collection means "no rules pinned" and yields nothing; that is distinct from
> passing None, which means "resolve now".

The optimization orchestrator already uses it in the required shape:

```python
facts = engine.facts(inp, result)
opportunities = await RulesEvaluatorService(session).evaluate(
    spec.tax_year, facts, pinned_rule_version_ids=...,
)
```

The scenario engine holds every input to those two calls already —
`applied.inputs`, the `TaxResult` it computes from them, and
`pinned.pinned_rule_version_ids`. **The blocker is not a missing capability; it
is a call that was never made.** No new eligibility authority is needed, and
none may be built.

**Finding 2 — the tax engine already computes the detail TAX_STATE parity needs.**
`TaxResult` carries `line_items: list[dict]`, populated on every evaluation. The
scenario computes a full `TaxResult` and persists only its totals, discarding
line items the engine already produced. §5's question — "does the scenario
discard deterministic tax-state detail the tax engine already calculated?" —
is answered **yes**. Parity requires sealing existing output, never a new
calculation.

**Finding 3 — RESOURCE is a genuine semantic mismatch, not a missing call.**
Detailed in §6 below. This is the one that does not resolve by making a call.

## 1. Node parity matrix

| node type | baseline authoritative source | counterfactual authoritative source | same semantic shape? | currently persisted? | must be sealed? | reconstructable without live fallback? | required change |
|---|---|---|---|---|---|---|---|
| `FACT` | live `finance.*` / `wealth.*` / `profile.tax_profile` rows | frozen `analysis_input_snapshot` + `ioe.scenario_input_change` | **NO** — see §2 | yes | no | yes | **NONE for the counterfactual; the mismatch is on the BASELINE side** |
| `TAX_STATE` | `analysis_run` header + `analysis_line_item` components | `scenario_result` totals only | **NO** — header vs header+components | totals only | **yes** | yes, once sealed | **EXTEND EXISTING ARTIFACT** — seal `TaxResult.line_items` |
| `OPPORTUNITY` | `ioe.optimization_candidate` | *(none — evaluation never runs)* | would be, if evaluated | **no** | **yes** | yes, once sealed | **NEW SEALED DETAIL** |
| `DEADLINE` | `rules.rule_deadline` via pinned versions | same table via the scenario's own pins, reached through counterfactual opportunities | **YES** | rule data already persistent | reference only | yes | **NONE beyond OPPORTUNITY** — see §4 |
| `EVIDENCE` (held document) | `docs.document` | identical — a simulation cannot make a person possess a document | **YES** | yes | no | yes | **NONE** |
| `EVIDENCE` (requirement) | `rules.rule_required_document` via pinned versions | same, reached through counterfactual opportunities | **YES** | rule data already persistent | reference only | yes | **NONE beyond OPPORTUNITY** — see §5 |
| `RESOURCE` | `ioe.resource_ledger_entry` (capacity / allocated / remaining) | *(no portfolio exists for a scenario)* | **NO** — ledger state vs requirement declaration | no | — | **no** | **BLOCKED — see §6** |
| `ASSUMPTION` | run `assumption_set` JSON | `ioe.scenario_assumption` | **YES** | yes | no | yes | **NONE** |
| `SCENARIO` | `ioe.scenario` | identical | **YES** | yes | no | yes | **NONE** |

## 2. FACT — the mismatch is on the baseline side

12A's `FACT` producer reads **live rows**: `finance.income_source`,
`finance.expense_record`, `wealth.asset`, `wealth.liability`,
`profile.tax_profile`, one node per row with `verification_status` and
provenance.

The counterfactual's only honest fact source is the **frozen snapshot** plus
`scenario_input_change`. But the snapshot is a canonicalized `TaxInput` — engine
input fields (`employment_income`, `rrsp_deduction`, …) — **not** per-row
declarations. There is no `income_source.id` inside it.

So a naive comparison would diff *per-row declarations* against *engine input
fields* and report every baseline FACT as `REMOVED`. That is precisely the §4
failure mode.

**Resolution, and it does not require new persistence:** the baseline for a
scenario comparison must itself be assembled from the **same frozen snapshot**
the scenario pinned, not from live rows. 12A anticipated this — `GraphView` was
declared with `CURRENT` and `HISTORICAL` and only `CURRENT` shipped. Both sides
then speak engine-input facts and the shapes match.

This also enforces §10/§11 for free: a baseline assembled from the pinned
snapshot **cannot** drift when live facts change, because it never reads them.

**Required change: none to persistence.** A snapshot-backed source bundle for
the baseline side, which is 12B continuation work.

## 3. TAX_STATE — seal what the engine already returned

Baseline emits an `analysis_run` node plus one node per `analysis_line_item`,
with `kind`, `label`, `amount`, `fact_key`, `tax_rule_version_id`. The
counterfactual currently has totals only, so every baseline line item would diff
as `REMOVED` — the §4 failure again, and the reason totals are not "already
semantically sufficient".

`TaxResult.line_items` is computed on every scenario evaluation and thrown away.
Sealing it is `EXTEND EXISTING ARTIFACT`, not new calculation, and it keeps
`TaxEngineService` the sole tax authority: the scenario stores what the engine
returned and computes nothing.

**Open question for implementation:** `analysis_line_item.tax_rule_version_id`
must survive into the sealed counterfactual line items, or `DERIVED_FROM` and
rule provenance lose their target. Whether `TaxResult.line_items` dicts carry a
rule version id has to be measured before the sealed shape is fixed; if they do
not, the parity is on `kind`/`label`/`amount` only and that limitation must be
stated rather than papered over.

## 4. DEADLINE — changes legitimately, without rule data changing

§10 is right to challenge the earlier "unchanged" assumption. 12A keys a
`DEADLINE` node by `rule_deadline.id` and reaches it through **the rule versions
the pinned candidates reference**. So if a counterfactual opportunity appears
that the baseline did not have, its rule version's deadlines appear too — a
legitimately `ADDED` deadline node, even though `rules.rule_deadline` itself did
not change.

The earlier 12B note's "always unchanged" was wrong, and it was wrong because it
reasoned about the *table* instead of the *reachability*. Deadlines are reached
through opportunities; opportunities change; therefore deadlines change.

`OpportunityContractV2.applicable_deadlines` already carries `DeadlineSpec`, so
no separate sealing is needed once the candidate set is sealed.

## 5. EVIDENCE — the split §9 demands, and it matters

**Held evidence** is genuinely unchanged. A simulated contribution does not make
the user possess a T4. 12A's held-document nodes carry only
`document_type_code`, and nothing in a scenario touches `docs.*`.

**Evidence requirements do change**, for the same reachability reason as
deadlines: 12A keys a requirement node by `(rule_version_id,
document_type_code)` and reaches it through candidates' rule versions. A new
counterfactual opportunity brings its required documents with it — and its
readiness resolves against the user's **unchanged** held documents, which is
exactly right: a simulation can create a new evidence *obligation* it cannot
satisfy, and `MISSING` is the truthful answer.

That is a genuinely useful product output and it falls out of the existing
readiness axis with no new persistence:
`OpportunityContractV2.required_documents` already carries `DocumentSpec`.

**So the blanket "EVIDENCE unchanged" claim in the earlier 12B note was wrong on
the requirement half.** §9 was right to force the re-check.

## 6. RESOURCE — the remaining blocker, and why sealing candidates does not fix it

12A's `RESOURCE` node is `ioe.resource_ledger_entry`:

```
resource_code · pool_scope · capacity · allocated · remaining
```

That is **portfolio ledger state** — how much of a shared pool a *set* of
selected candidates consumed, and what is left. It is meaningful only because
portfolio assembly allocated a pool across multiple candidates in an order.

What a scenario can authoritatively produce is
`OpportunityContractV2.shared_resource_codes` plus `ActionSpec` cost fields:
a **requirement declaration** — "this opportunity draws on RRSP room" — with no
capacity, no allocation and no remainder, because nothing allocated anything.

These are not the same concept, and §4 forbids comparing them as though they
were. Sealing the candidate set does **not** resolve it.

Three options, and the recommendation:

1. **Invoke portfolio optimization for scenarios.** §12 forbids this outright,
   and rightly — it would make `ScenarioService` depend on optimization and turn
   Before-You-Act into a hidden optimizer. **Refused.**
2. **Seal a requirement-only resource concept and give it a distinct node type
   or an explicit shape marker**, so a comparison never diffs a requirement
   against a ledger entry. Honest, but it adds a node concept, and §39 fixes the
   taxonomy at eight live types.
3. **Declare `RESOURCE` out of scope for baseline-vs-counterfactual comparison**,
   with the reason recorded: a single scenario has no portfolio, so it has no
   ledger state, and the comparison layer must exclude the family rather than
   fabricate parity. Shared-resource *requirements* still travel as an attribute
   of the counterfactual opportunity, so no information is lost — only the false
   parity.

**Recommended: option 3.** It is the only one that neither invents an authority
nor widens the taxonomy, and §13 anticipates exactly this outcome ("If no: do
not manufacture a Resource node"). It means 12B's comparison contract must drop
`resource_changes` as a top-level family and carry shared-resource requirements
inside opportunity changes instead.

This is a **POLICY_DECISION_REQUIRED**: it narrows a comparison family you
listed in 12B §7, so it should be your call rather than mine.

## 7. Support-score parity — a second open question

12A's `OPPORTUNITY` node carries five support fields
(`raw_support_score`, `assumption_adjusted_score`, `display_support_score`,
`support_cap_applied`, `support_cap_reason_code`) sourced from
`optimization_candidate`, where the IOE computes them **per candidate**.

`RulesEvaluatorService` does not produce support scores; the IOE computes them
downstream via `support.compute(evidence_status, calculation_basis,
assumptions)`. The scenario currently computes exactly **one** support breakdown
for the whole result, not one per candidate.

So per-candidate support parity requires the scenario to run the same
`support.compute()` per counterfactual candidate — reusing the existing support
domain, not a new one. That is feasible and in-scope, but it must be explicit in
the sealed shape rather than discovered, or the comparison will report
`SUPPORT_CHANGED` for every candidate purely because one side has nulls.

## 8. Edge parity

| edge | counterfactual semantics | verdict |
|---|---|---|
| `DERIVED_FROM` | line item → run, once line items are sealed | **parity after §3** |
| `REQUIRES` | opportunity → evidence requirement | **parity after candidate sealing** |
| `SUPPORTED_BY` | requirement → held document (held side unchanged) | **parity** |
| `EXPIRES_AT` | opportunity → deadline | **parity after candidate sealing** |
| `ASSUMES` | scenario → assumption | **parity today** |
| `REFERENCES_SCENARIO` | scenario → tax state | **parity today** |
| `INELIGIBLE_BECAUSE` | baseline: `portfolio_exclusion.blocking_candidate_id` | **NO counterfactual semantics** — blocking is a portfolio outcome; a scenario has no portfolio |
| `CONSTRAINED_BY` | baseline: exclusion → resource ledger | **NO** — same reason as `RESOURCE` |
| `CONSUMES_RESOURCE` | baseline: member allocations → ledger | **NO** — same reason |
| `CONFLICTS_WITH` | baseline: `recommendation_relationship`, a run-scoped pairwise table | **NO counterfactual semantics** — not produced for scenarios |
| `REFERENCES_RULE` | reserved in 12A, stays reserved | **N/A** |

Four edge families have no counterfactual meaning, and all four are portfolio
concepts. That is the same finding as §6 seen from the edge side, and it is
consistent rather than coincidental: **a scenario is one action, a portfolio is
a chosen set of actions.** The comparison layer must compare what a scenario is,
not what a portfolio is.

`ELIGIBLE_BECAUSE`, `DEPENDS_ON`, `AFFECTS` and `EVIDENCED_BY` appear in the
12B1 §42 list but are **not live edge types in 12A** — the shipped taxonomy has
ten live types and one reserved. `eligibility_basis_codes` and `dependencies`
travel as opportunity attributes, which is where 12A put rule provenance.

## 9. Resulting implementation scope

Ordered, with the two blockers separated from the buildable work.

**Buildable, and smaller than the first analysis suggested:**

1. Seal `TaxResult.line_items` alongside the existing scenario totals (§3).
2. Call `RulesEvaluatorService.evaluate(tax_year, facts,
   pinned_rule_version_ids=pinned.pinned_rule_version_ids)` with counterfactual
   facts, and seal the resulting `OpportunityContractV2` set (§0, finding 1).
3. Per-candidate `support.compute()` for score parity (§7).
4. Canonicalize and domain-hash the sealed derived state; bind it into the
   result hash behind a bumped `result_schema_version` so legacy scenarios keep
   verifying under their own contract (§26/§27).

**A structural constraint that shapes step 2.** `_compute` is deliberately
session-free — *"There is no session in scope here, so the compute phase has no
route to live data even by mistake."* `RulesEvaluatorService` needs a session.
The evaluation therefore belongs in TX-2 alongside persistence and the seal,
which is exactly the ordering §23 requires. `engine.facts_for(inp, result)` is
pure and stays in `_compute`, so the counterfactual fact map is produced without
a session and consumed with one. **`_compute` must not acquire a session.**

**Blockers requiring your decision before 12B resumes:**

* **RESOURCE** (§6) — recommend excluding the family from comparison.
* **Support-score shape** (§7) — cheap, but must be decided rather than
  defaulted.

## 10. Storage decision — deferred deliberately

§15 requires comparing scenario-owned child rows against a canonical sealed
payload against reuse of an existing structure, on immutability, replay,
hashing, payload size, queryability, RLS, privacy lifecycle, write cutoff,
migration complexity, historical compatibility and performance.

That comparison depends on the sealed shape, and the sealed shape depends on the
two decisions above — particularly whether per-candidate support scores are
included, which materially changes row width and payload size. Choosing storage
first would be choosing before knowing what is stored.

What is already clear: whichever option lands, **the privacy universe moves from
70 to 71 or stays at 70**, and the answer must be measured and every new table
fully classified with zero `UNCLASSIFIED_BLOCKING` before closure. No table will
be added for convenience, and none avoided at the cost of an opaque artifact.
