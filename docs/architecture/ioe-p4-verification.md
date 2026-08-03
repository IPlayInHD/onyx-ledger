# IOE Phase P4 — Verification Record

Closes the six verification items raised on P4 approval. Every figure here was
measured against live PostgreSQL 16, not asserted from design intent.

- Migrations at time of record: `0030_ioe_portfolio` … `0033_ioe_cost_and_reeval`
- Test suite: 328 passed, 1 skipped
- Autogenerate clean after a full `downgrade base` → `upgrade head` rebuild

---

## 1. Index coverage and restricted-role query plans

### Method

A benchmark database (`onyx_bench`) was seeded to a realistic multi-tenant
shape so the planner is choosing between real alternatives rather than
seq-scanning a handful of rows regardless of indexing:

| Table | Rows | Tenants |
|---|---:|---:|
| `optimization_run` | 2,000 | 400 users × 5 runs |
| `optimization_candidate` | 50,000 | 25 per run |
| `strategy_portfolio` | 2,000 | 1 per run |
| `portfolio_evaluation_step` | 50,000 | |
| `portfolio_exclusion` | 40,000 | |
| `score_component` | 250,000 | |
| `candidate_cost` | 50,000 | |

Every plan below was captured as **`onyx_bench`**, a login role holding only
`onyx_app_rw` — the restricted runtime role, not the table owner — with
`app.user_id` set exactly as `unit_of_work()` sets it. All plans are therefore
RLS-filtered plans under the migration 0032 policies.

### Index coverage of every ownership traversal

Every column an RLS policy resolves through has an index leading on it:

| Table | Traversal column | Leading index |
|---|---|---|
| `optimization_candidate` | `run_id` | `ix_ioe_candidate_run` |
| `optimization_run_event` | `run_id` | `ix_ioe_run_event` |
| `recommendation_relationship` | `run_id` | `ix_ioe_relationship_run` |
| `run_rule_version` | `run_id` | `run_rule_version_run_id_tax_rule_version_id_key` |
| `strategy_portfolio` | `run_id` | `strategy_portfolio_run_id_key` |
| `multi_year_projection` | `run_id` | `ix_ioe_projection_run` |
| `candidate_cost` | `candidate_id` | `ix_ioe_cost_candidate` |
| `candidate_economic_effect` | `candidate_id` | `ix_ioe_effect_candidate` |
| `confidence_component` | `candidate_id` | `confidence_component_candidate_id_factor_code_key` |
| `score_component` | `candidate_id` | `score_component_candidate_id_factor_code_key` |
| `portfolio_member` | `portfolio_id` | `portfolio_member_portfolio_id_apply_order_key` |
| `portfolio_evaluation_step` | `portfolio_id` | `ix_portfolio_step` |
| `portfolio_exclusion` | `portfolio_id` | `ix_portfolio_exclusion` |
| `resource_ledger_entry` | `portfolio_id` | `resource_ledger_entry_portfolio_id_resource_code_key` |
| `scenario_event` | `scenario_id` | `ix_ioe_scenario_event` |
| `scenario_input_change` | `scenario_id` | `scenario_input_change_scenario_id_apply_order_field_key` |
| `scenario_result` | `scenario_id` | `scenario_result_scenario_id_key` |
| `optimization_run` *(chain root)* | `user_id` | `ix_ioe_run_user_year` |
| `scenario` *(chain root)* | `user_id` | `ix_ioe_scenario_user` |

This table is not maintained by hand:
`test_ownership_traversal_columns_are_indexed` asserts it against `pg_index`.

### Measured plans (`EXPLAIN (ANALYZE, BUFFERS)`)

| # | Read | Access method | Rows | Exec time |
|---|---|---|---:|---:|
| A | portfolio by run | Index Scan `strategy_portfolio_run_id_key` | 1 | 0.077 ms |
| B | candidates by run | Bitmap Heap Scan → `ix_ioe_candidate_run` | 25 | 0.267 ms |
| C | relationships by run | Index Scan `ix_ioe_relationship_run` | 24 | 0.056 ms |
| D | trace by portfolio | Index Scan `ix_portfolio_step` | 25 | 0.262 ms |
| E | exclusions by portfolio | Index Scan `ix_portfolio_exclusion` | 20 | 0.118 ms |
| F | members by portfolio | Index Scan `portfolio_member_portfolio_id_apply_order_key` | 5 | 0.095 ms |
| G | score components by candidate *(grandchild, 2 hops)* | Index Scan `score_component_candidate_id_factor_code_key` | 5 | 0.106 ms |
| H | scenario result by scenario | Index Scan `scenario_result_scenario_id_key` | 1 | 0.053 ms |

No sequential scan appears in any keyed read. The policy predicate is evaluated
either as a `hashed SubPlan` (the user's run-id set is built once from
`ix_ioe_run_user_year` and probed per row) or as a per-row PK lookup on
`optimization_run_pkey`. Two-hop chains (G) resolve entirely through primary-key
index scans on each parent.

### Finding: unqualified listings are O(child table)

Plan I — `SELECT count(*) FROM ioe.strategy_portfolio` with no `run_id`
predicate — is a **Seq Scan with `Rows Removed by Filter: 1995`** (0.365 ms at
this volume). Forcing `enable_seqscan = off` produces an Index Only Scan that
*still* filters all 2,000 rows.

This is inherent, not a missing index: the policy predicate is an `EXISTS`
subquery over a different table, so it can never become a searchable index
condition on the child. The consequence is recorded rather than worked around:

> **RLS is the correctness boundary, not the access path.** Every read of IOE
> evidence must be qualified by its parent key (`run_id`, `portfolio_id`,
> `candidate_id`). A query that omits it is still correct — it returns only the
> caller's rows — but its cost scales with the whole table rather than with the
> caller's data.

This is a constraint on the API phase, carried forward in §7.

---

## 2. Tenant-isolation inventory

Every table in schema `ioe` is classified. The inventory is enforced by
`tests/integration/test_ioe_rls_inventory.py`, which reads `pg_class`,
`pg_policies` and `pg_index` directly — a table added in a later phase without
an ownership policy fails the suite.

### User-derived evidence — RLS enabled, forced, deny-by-default

| Table | Ownership chain |
|---|---|
| `optimization_run` | `user_id` (direct) |
| `scenario` | `user_id` (direct) |
| `optimization_candidate` | `run_id` → `optimization_run.user_id` |
| `optimization_run_event` | `run_id` → `optimization_run.user_id` |
| `recommendation_relationship` | `run_id` → `optimization_run.user_id` |
| `run_rule_version` | `run_id` → `optimization_run.user_id` |
| `strategy_portfolio` | `run_id` → `optimization_run.user_id` |
| `multi_year_projection` | `run_id` → `optimization_run.user_id` |
| `candidate_cost` | `candidate_id` → `optimization_candidate` → run |
| `candidate_economic_effect` | `candidate_id` → `optimization_candidate` → run |
| `confidence_component` | `candidate_id` → `optimization_candidate` → run |
| `score_component` | `candidate_id` → `optimization_candidate` → run |
| `portfolio_member` | `portfolio_id` → `strategy_portfolio` → run |
| `portfolio_evaluation_step` | `portfolio_id` → `strategy_portfolio` → run |
| `portfolio_exclusion` | `portfolio_id` → `strategy_portfolio` → run |
| `resource_ledger_entry` | `portfolio_id` → `strategy_portfolio` → run |
| `scenario_event` | `scenario_id` → `scenario.user_id` |
| `scenario_input_change` | `scenario_id` → `scenario.user_id` |
| `scenario_result` | `scenario_id` → `scenario.user_id` |

All nineteen carry `ENABLE ROW LEVEL SECURITY` **and**
`FORCE ROW LEVEL SECURITY`, so the table owner is subject to the policy too.
Every policy's `USING` **and** `WITH CHECK` clause resolves to
`ref.current_app_user()`.

### Shared reference data — deliberately not user-scoped

`rule_snapshot`, `rule_snapshot_artifact`, `run_rule_snapshot`, `weight_config`,
`assumption_set`, `assumption`. These hold published rule snapshots, scoring
weights and the assumption vocabulary. They contain no user-derived data and are
readable by design.

### Deny-by-default, measured

`ref.current_app_user()` reads `app.user_id` and returns NULL when it is unset;
every ownership predicate is then false. Measured on the seeded benchmark (2,000
runs / 50,000 candidates / 250,000 score components):

| Context | runs | portfolios | candidates | steps | exclusions | costs | score components | scenario results |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `app.user_id` = a real tenant | 5 | 5 | 125 | 125 | — | — | 625 | — |
| `app.user_id` unset | **0** | **0** | **0** | **0** | **0** | **0** | **0** | **0** |

A missing `SET app.user_id` is therefore a hard failure, never a silent
full-table read. `test_deny_by_default_when_app_user_id_is_unset` asserts rows
exist first, so the check can never pass merely because the tables are empty.

---

## 3. Deterministic legacy → P4 cost normalization

Migration 0031 lets rules author the separated taxonomy directly. Existing rule
data still speaks the legacy vocabulary, in which `required_cash_contribution`
conflated two different economic facts. Resolution is deterministic, ordered,
and records its basis — implemented in
`app/services/ioe/domain/cost_taxonomy.py`, version **`1.0.0`**.

| Order | Authored value | Resolves to | `cost_type_source` |
|---|---|---|---|
| 1 | any P4 value (`liquidity_commitment`, `asset_transfer`, `nonrecoverable_expenditure`, `implementation_cost`) | itself | `authored_verbatim` |
| 2 | `required_expenditure` | `nonrecoverable_expenditure` | `legacy_exact_synonym` |
| 3 | `required_cash_contribution` **with** a registry declaration | the lever's `commitment_class` | `lever_registry` |
| 4 | `required_cash_contribution` **without** one | `liquidity_commitment` | `legacy_conservative_default` |

**Rule data always wins.** The registry is consulted only for the one genuinely
ambiguous legacy value, and only through the pinned, versioned lever registry
(`LEVER_REGISTRY_VERSION = 1.1.0`) — the same authority that already owns what
an action does. Nothing is inferred from a rule's name, category, or economic
effect type.

### Contributions are not all one type

| Lever | `commitment_class` | Reasoning |
|---|---|---|
| `INCREASE_RRSP_DEDUCTION` | `asset_transfer` | value retained, inside a registered plan |
| `INCREASE_FHSA_DEDUCTION` | `asset_transfer` | value retained |
| `INCREASE_DONATIONS` | `nonrecoverable_expenditure` | money leaves for good |
| `INCREASE_MEDICAL_EXPENSES` | `nonrecoverable_expenditure` | money spent |
| `INCREASE_CHILDCARE` | `nonrecoverable_expenditure` | money spent |
| `INCREASE_TUITION` | `nonrecoverable_expenditure` | money spent |
| `INCREASE_BUSINESS_EXPENSES` | `nonrecoverable_expenditure` | money spent |
| everything else | *(undeclared)* | falls to rule 4 |

The behavioural consequence: an RRSP contribution constrains feasibility without
reducing the objective, while an equal-dollar donation reduces it. Under the
pre-P4 vocabulary both behaved as the former.

### Why rule 4 resolves to `liquidity_commitment`

It reproduces pre-P4 behaviour exactly — a feasibility constraint that is not a
loss — so nothing new is claimed about money the rule never classified.
Defaulting to `nonrecoverable_expenditure` would invent an expense and
understate the user's position.

### Provenance is persisted, not just computed

Migration 0033 adds to `ioe.candidate_cost`:

- `authored_cost_type` — the rule's value, verbatim
- `cost_type_source` — CHECK-constrained to the four basis codes above
- `taxonomy_version` — the resolution rules that produced `cost_type`

`cost_taxonomy_version` is in the run version manifest, so a change to the
resolution rules changes run identity.

Tests: `tests/unit/ioe/test_cost_taxonomy.py` (12),
`test_cost_normalization_provenance_is_persisted`.

---

## 4. Eligibility-changing portfolio actions

An earlier action can move the very facts a later candidate's eligibility was
determined from — an RRSP contribution lowers net income, and a rule gated on
net income may stop or start matching.

### Order of preference

1. **Re-evaluate** against the same pinned rule-version set.
2. If the answer cannot be resolved, exclude with an explicit
   `requires_re_evaluation` result — never assume either answer.

### Why no newly published rule can enter an in-flight run

The proof is structural rather than procedural:

- `load_pinned_condition_trees(session, spec.pinned_rule_version_ids)` runs
  **once**, during compute, and its query is `WHERE rule_version_id IN (pinned)`.
- `PinnedEligibilityRechecker` is constructed from that dict and **holds no
  session**. It has no attribute through which a query could be issued, so there
  is no code path from re-evaluation back to the rules tables.
- A candidate whose `rule_version_id` is absent from the pinned dict returns
  `INDETERMINATE` — it cannot be admitted on an unpinned rule.

`test_rechecker_only_ever_sees_the_pinned_versions` publishes a new rule after
pinning and asserts it is absent from the loaded trees and that a candidate on
it resolves to `INDETERMINATE`. This complements the P3 race test, which proves
the same property for the initial evaluation.

### Fact parity

Re-evaluation recomputes facts through `TaxEngineService.facts_for` — the same
function the initial evaluation uses, extracted as a static method rather than
duplicated. `test_re_evaluation_uses_the_same_facts_the_first_evaluation_used`
asserts the two maps are equal.

### Tri-state result

| Verdict | Membership | Reason code | Resolution options |
|---|---|---|---|
| `eligible` | proceeds to trial | — | — |
| `ineligible` | `excluded_constraint` | `ELIGIBILITY_CHANGED_BY_EARLIER_ACTION` | `REVIEW_EARLIER_ACTION`, `RE_RUN_WITHOUT_IT` |
| `indeterminate` | `requires_re_evaluation` | `REQUIRES_RE_EVALUATION` | `RE_RUN_OPTIMIZATION`, `REVIEW_EARLIER_ACTION` |

`requires_re_evaluation` is its own membership value, added to the CHECK on both
`optimization_candidate.portfolio_membership` and
`portfolio_exclusion.membership` by migration 0033, plus a
`requires_re_evaluation` boolean and `re_evaluation_reason_code` on the
candidate. "We could not tell" never reads as "we checked and it failed", and
such a candidate is never a portfolio member.

The recheck runs only once at least one action has been applied — with nothing
selected, no fact has moved, and the original determination still stands.

---

## 5. The portfolio objective, exactly

**Code** `current_year_tax_reduction_net_of_expenditure`
**Version** `1.0.0`
**Implementation** `savings.objective_cost()`
**Persisted on** `ioe.strategy_portfolio.portfolio_objective_code` /
`portfolio_objective_version`, and in the run version manifest.

### Formula

The objective is expressed as a quantity to **minimize**:

```
objective_cost = current_tax
               + Σ amount WHERE cost_type ∈ {nonrecoverable_expenditure,
                                             required_expenditure}
               + liquidity_penalty
```

where

```
liquidity_penalty = max(0, Σ amount WHERE cost_type ∈ LIQUIDITY_COMMITMENT_TYPES
                             − available_cash)      # 0 when available_cash is None
```

### Sign convention

```
objective_delta = baseline_objective_cost − final_objective_cost
```

A **positive delta is an improvement**. Expressing the objective as a cost
rather than a benefit is what makes that formula and that reading agree; a
benefit-shaped objective would need the subtraction the other way round and
would silently invert every stored delta. Acceptance requires the cost to
*fall*: `state.objective − trial_objective ≥ EPSILON_ACCEPT`.

### Field sources

| Term | Source | Never |
|---|---|---|
| `current_tax` | `TaxResult.total_payable` from a **tax engine run** on the hypothetical inputs | never estimated, never summed from recommendations |
| nonrecoverable costs | `CostComponent.amount` from rules-authored `rule_action.cost_amount` | never imputed |
| cost classification | §3 resolution | never inferred from rule name or category |
| `available_cash` | user-supplied constraint | never assumed |

Liquidity commitments and asset transfers carry **weight 0** in the objective —
they constrain feasibility but are not losses, entering only via the penalty
when they exceed declared available cash.

### Decimal policy and the single rounding point

- All arithmetic in `Decimal`. No float appears anywhere in the path;
  `EPSILON_ACCEPT = Decimal("0.01")` is a Decimal literal.
- **Scale** `Decimal("0.01")` (2 dp), matching `ref.money_amt` = `NUMERIC(14,2)`.
- **Mode** `ROUND_HALF_UP`.
- **Rounding point: once, at the end.** `objective_cost()` quantizes its return
  value; intermediate sums inside it are unrounded. `objective_delta`,
  `standalone_potential`, `incremental_portfolio_benefit`, `sum_of_standalone`
  and `interaction_delta` are each quantized exactly once, at the same stage,
  with the same scale and mode.

Standalone, incremental, and final combined results all use this same objective,
the same baseline, the same sign convention, the same Decimal policy, and the
same rounding stage. That is what makes the invariants in §6 meaningful rather
than coincidental.

---

## 6. Three-way reconciliation of the stored delta

Asserted on rows **read back from the database**, not in memory — a headline
that only reconciles before it is written is not one anyone can audit.

```
stored objective_delta
  == Σ portfolio_member.incremental_benefit   (exact apply order)
  == objective_value_baseline − objective_value_final
  == portfolio_total_benefit                  (the figure the user is shown)
```

All four quantized with `Decimal("0.01")` / `ROUND_HALF_UP` — the §5 policy,
applied identically to each side.

- **I-1** (telescoping) holds structurally: each increment is
  `state.objective − trial_objective` at acceptance, so the sum telescopes to
  `baseline − final` exactly.
- **I-2** (combined-run reconciliation) re-runs the engine on the final input
  state and requires agreement with the last accepted trial within
  `EPSILON_ACCEPT`.

Both are **hard failures**. A portfolio that cannot reconcile raises
`PortfolioReconciliationError`, which the orchestrator classifies as
`PORTFOLIO_ASSEMBLY_FAILED` and the run fails — an unverifiable total is never
shown.

### No inequality is imposed

`interaction_delta = sum_of_standalone − objective_delta` is recorded and
classified (`additive` / `sub_additive` / `super_additive`). **No constraint
requires the portfolio total to be ≤ the sum of standalone values.** Both
super-additive and sub-additive interactions are valid results;
`sum_of_standalone` is a diagnostic and must never be displayed as a total.

Tests: `test_stored_objective_delta_reconciles_three_ways`,
`test_no_inequality_is_imposed_between_total_and_sum_of_standalone`.

---

## 7. Carried into the API phase

Recorded here so they are not rediscovered later:

1. **Read the sealed evidence; never recompute.** API responses must read
   `strategy_portfolio`, `portfolio_member`, `portfolio_evaluation_step`,
   `portfolio_exclusion` and `resource_ledger_entry` as stored. No endpoint may
   recompute a total, re-run the engine to fill a field, or sum recommendation
   amounts. The only displayable total is `portfolio_total_benefit`.
2. **Always qualify by the parent key** (§1 finding). RLS guarantees
   correctness regardless, but an unqualified read costs a full child-table
   scan.
3. **`sum_of_standalone` is never displayed.** It is a diagnostic for the
   interaction classification.
4. **Support scores are not probabilities.** Surface
   `display_support_score` with `SUPPORT_SCORE_DISCLAIMER`; never present it as
   a probability of CRA acceptance or of receiving the displayed amount.
5. **`optimality_claim` is `none`.** No surface may describe the result as
   optimal, best, or maximal — it is a feasible, deterministic, engine-evaluated
   strategy portfolio.
6. **Excluded candidates are part of the result.** Surface them with their
   reason code and resolution options; hiding them makes the portfolio look like
   the whole opportunity set.
7. **Derived cost types are labelled.** Where `cost_type_source` is not
   `authored_verbatim`, the classification is an IOE derivation from a legacy
   rule value, not rule data.
