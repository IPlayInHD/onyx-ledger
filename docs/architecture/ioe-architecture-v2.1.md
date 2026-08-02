# Onyx Ledger — IOE Architecture, Revision 2.1 (targeted amendments)

**Status:** Pre-implementation. Amends Revision 2 in six areas only. No implementation code.
**Relationship to Revision 2:** `ioe-architecture-v2.md` remains in force in full. This document **replaces** the specific subsections named below and adds the tables/enums/invariants they require. Everything not named here is unchanged and is not re-opened.

**Scope of this revision (conditional-acceptance items):**

| # | Area | Amends |
|---|---|---|
| A | Rule-snapshot pinning | v2 §9.4 (manifest), §7 (persistence), §24 (staleness) |
| B | Portfolio objective metrics | v2 §12.3 (acceptance rule), §12.2 (measures) |
| C | Registry-controlled lever bindings | v2 §6.2 (contract field), §17 (lever registry) |
| D | Greedy-portfolio limitations | v2 §12.1–12.3 |
| E | Interaction-delta mathematics | v2 §12.2 |
| F | Migration ordering | v2 §30, §31 |

---

## A. Rule-snapshot pinning

### A.1 The defect

Revision 2 pinned a **rule-version id set** (`ioe.run_rule_version`, `rule_version_set_hash`) and an opaque `reference_data_version` string. Two gaps make that insufficient for reproducible replay:

1. **A version id is not its content.** `tax_kb.tax_rule_version` rows are immutable by TKMS policy, but the artifacts that determine the *computed* result hang off shared tables that are **not** provably immutable: `rules.calc_formula.expression`, `rules.calc_formula_input`, `rules.rule_condition[_group]`, `rules.rule_outcome`, and `rules.calc_constant` (keyed `(code, tax_year)` — reference data, not version-scoped). Editing a shared formula changes historical replay silently.
2. **`reference_data_version` pins nothing.** Verification against the codebase: the pure engine's brackets and rates live in `app/services/tax_engine/core/data.py` as a hardcoded module (`FEDERAL_2025 = FederalData(...)`), with **no version constant**. Reference data is therefore versioned only by the deployed artifact, and a redeploy that corrects a bracket would alter replay with no manifest change to detect it.

### A.2 Correction — content-addressed rule snapshots

Every run pins a **rule snapshot**: the canonical content (§9.1 serialization) of every artifact actually read, hashed per artifact and in aggregate.

```text
rule_snapshot_artifact_kind:
  tax_rule_version | calc_formula | calc_formula_input
  rule_condition_group | rule_condition | rule_outcome
  calc_constant | contribution_limit | tax_bracket_set | tax_bracket
  engine_reference_dataset          # content hash of the engine's in-code reference data
  fact_definition
```

**New tables (in the `ioe` schema, added to v2 §7):**

| Table | Kind | Key columns |
|---|---|---|
| `ioe.rule_snapshot` | **I** | id, snapshot_hash (UNIQUE), artifact_count, created_at |
| `ioe.rule_snapshot_artifact` | **I** | snapshot_id, artifact_kind, artifact_id (nullable for in-code datasets), artifact_key (text, e.g. `MEDICAL_FLOOR_RATE:2025`), content_hash, content `jsonb` (nullable — see D-10) |
| `ioe.run_rule_snapshot` | **I** | run_id / scenario_id, snapshot_id |

`ioe.rule_snapshot` is **content-addressed and shared**: identical rule content across many runs yields one row (`snapshot_hash` UNIQUE), so storage is bounded by distinct rule states, not by run count. `ioe.run_rule_version` from v2 §7 is retained (it remains the human-readable "which rules applied" list) but is no longer the pinning mechanism.

**Manifest change (replaces the v2 §9.4 rows `rule_version_set_hash` and `reference_data_version`):**

| Manifest key | Pins |
|---|---|
| `rule_snapshot_hash` | aggregate hash over all artifacts (supersedes `rule_version_set_hash`) |
| `engine_reference_data_hash` | content hash of the engine's in-code reference dataset |
| `engine_reference_data_version` | declared constant (see A.4) |

`rule_snapshot_hash` is computed as the hash of the canonically serialized, **sorted** list of `(artifact_kind, artifact_key, content_hash)` triples — array ordering explicit per §9.1.

### A.3 Replay verification and drift

Snapshots make drift **detectable and reportable** rather than silent:

```text
replay_status:
  verified      # recomputed artifact hashes match the pinned snapshot exactly
  drifted       # one or more artifacts changed since the run
  unavailable   # an artifact no longer exists (withdrawn/deleted)
```

`GET /ioe/optimizations/{id}/trace` returns `replay_status` plus `drifted_artifacts[] {artifact_kind, artifact_key, pinned_hash, current_hash}`. A drifted historical run is **never** silently re-presented as current: it is labelled, and the stored immutable evidence stands as the record of what was computed at the time.

Where `content jsonb` is materialized (D-10), a drifted run remains **replayable from the snapshot**; where only hashes are stored, drift is detectable but exact replay is not possible. This is stated honestly rather than promised away.

### A.4 Required additive change to the engine module

Two small additions to `app/services/tax_engine/core/data.py` — **no calculation change, no engine redesign**:

- `REFERENCE_DATA_VERSION: str` — a declared constant, bumped whenever the dataset is edited.
- The dataset is hashed at runtime by a pure IOE function that canonically serializes the already-public dataclasses (`FederalData`, provincial structures). The engine does not import the IOE; the IOE reads the engine's public data.

Because this touches an approved, stable module, it is raised as **D-11** rather than assumed.

> **Not proposed:** injecting a snapshot back into the engine so it computes *from* pinned data. That would restructure the engine (currently reference data is in-code) and is out of scope. Consequence, stated plainly: replay after a reference-data change is **detectable but not re-computable** until such a seam exists. Deferred with a defined seam (`engine_reference_provider`), not claimed.

### A.5 Staleness linkage (amends v2 §24)

`RULE_VERSION_REPLACED`, `RULE_WITHDRAWN`, and `REFERENCE_DATA_CHANGED` are now evaluated by **comparing `rule_snapshot_hash`**, not by comparing version-id sets — so a content edit that preserves version ids still marks dependent runs stale. New reason code: `RULE_CONTENT_DRIFTED`.

---

## B. Portfolio objective metrics

### B.1 The defect

Revision 2 §12.3 accepted a candidate iff `trial_tax < prev_tax`. That is an *implicit, unstated* objective — minimize current-year total payable — and it is wrong in three ways: it ignores what the action costs, it credits a **deferral** as though it were a permanent reduction, and it gives recurring/multi-year benefits no weight at all. An assembler without a declared objective cannot be said to optimize anything.

### B.2 Correction — an explicit, versioned objective function

```text
objective_metric:
  current_year_tax_reduction                    # gross reduction in total_payable
  current_year_tax_reduction_net_of_expenditure # DEFAULT (D-13)
  net_cash_benefit_current_year                 # reduction − expenditure − implementation cost
  comparable_value_multi_horizon                # §13.3 comparable value across horizons
```

**Cost semantics — the distinction that matters.** A `required_cash_contribution` (RRSP/FHSA) is **not** a cost: the asset is retained, only liquidity is consumed. A `required_expenditure` (donation, eligible expense) **is** a cost: the money is gone. Treating them alike would make contribution strategies look artificially expensive and expenditure strategies artificially cheap.

```text
objective_value(portfolio_input) =
      Σ_effects  amount × effect_factor × horizon_discount        # §13.3 policy, versioned
    − Σ_costs    required_expenditure + implementation_cost       # true costs
    − liquidity_penalty(Σ required_cash_contribution)             # constraint, not a cost
                                                                  # (0 while within declared available cash)
```

Deferral enters only through `effect_factor[tax_deferral] ≪ 1.00` (D-5), so a timing shift can never be scored as a permanent reduction.

**Acceptance rule (replaces `trial_tax < prev_tax`):**

```text
accept candidate iff  Δobjective ≥ epsilon_accept          # Decimal('0.01'), money scale 2
```

All comparisons are `Decimal` at the declared scale — never float. `epsilon_accept` is part of `portfolio_objective_version`.

### B.3 Always-reported metrics

Regardless of the chosen objective, `ioe.strategy_portfolio` records and the API returns:

| Metric | Meaning |
|---|---|
| `objective_metric`, `objective_version` | which objective governed assembly |
| `objective_value_baseline`, `objective_value_final` | objective at start and end |
| `portfolio_total_benefit` | engine-verified current-year tax reduction (v2 §12.2) |
| `total_required_cash_contribution` | liquidity required (asset retained) |
| `total_required_expenditure`, `total_implementation_cost` | true costs |
| `net_current_year_benefit` | `portfolio_total_benefit − expenditure − implementation_cost` |
| `total_deferral_amount` | reported **separately**, never added to reduction |
| `total_recurring_annual`, `total_multi_year_projected` | reported separately, by horizon |
| `benefit_efficiency` | `portfolio_total_benefit ÷ max(total_required_cash_contribution, 1)` |
| `total_effort_score`, `reversibility_profile` | implementation burden and lock-in |

The user-facing headline remains `portfolio_total_benefit` from the combined engine run (v2 §12.2, unchanged). The objective is an **assembly-time selection criterion**, not a displayed dollar figure.

> **D-13:** confirm `current_year_tax_reduction_net_of_expenditure` as the MVP default, with the others exposed as alternates in a later release. Multi-objective portfolios (producing several candidate portfolios) are explicitly out of MVP scope.

---

## C. Registry-controlled lever bindings

### C.1 The defect

Revision 2 §6.2 gave the contract a field `portfolio_application: LeverBindingSpec | null`, described as "how to apply it in a portfolio run." That lets **rule data specify mutations to engine inputs** — a boundary violation with two consequences: rule authors could write arbitrary `TaxInput` field changes (an unbounded, unsafe surface), and lever semantics would live in two places (rule data *and* the IOE lever registry), which cannot both be authoritative.

### C.2 Correction — rules reference levers; the registry defines them

**Contract v2 field change (replaces `portfolio_application`):**

```text
portfolio_lever_ref: {
    lever_code:         str            # MUST exist in the IOE lever registry
    parameter_bindings: {str: str}     # parameter name → source key (e.g. 'amount' → 'rule.max_amount')
} | null
```

The rules layer supplies **a reference and parameters only**. It never names a `TaxInput` field. The **IOE lever registry is the sole authority** for what a lever does.

**Lever registry entry (versioned, `lever_registry_version`):**

```text
lever_registry_entry:
  lever_code             str            # 'INCREASE_RRSP_DEDUCTION'
  version                str
  writable_fields        list[str]      # ALLOW-LIST of TaxInput fields this lever may write
  direction              increase | decrease | set
  parameter_schema       {name: {type, min, max, required}}
  bounds                 {field: {min, max}}
  shared_resource_code   str | null     # links to the §12.4 ResourceLedger
  portfolio_eligible     bool
  composite_children     list[lever_code]   # composite levers apply children atomically
  conflicts_with         list[lever_code]
```

**Enforcement (defence in depth):**

1. **Allow-list at application.** `levers.apply()` writes **only** fields in `writable_fields`; any attempt to touch another field raises. Asserted at runtime and covered by a dedicated test.
2. **Parameter validation.** Values are validated against `parameter_schema` and `bounds` before application; violations mark the candidate `excluded_not_evaluable` with a reason code.
3. **Unknown code.** A rule referencing an unregistered `lever_code` yields `excluded_not_evaluable` (reason `UNKNOWN_LEVER_CODE`) — never a guessed mapping.
4. **Publish-time cross-check.** TKMS validation gains a check that any `portfolio_lever_ref.lever_code` in rule data exists in the registry at the pinned `lever_registry_version`, so the mismatch surfaces at publish rather than at runtime. *(Additive TKMS validation check — no new schema.)*
5. **Registry is code + seed data**, versioned and pinned per run; rule data cannot extend it.

This keeps the boundary clean: the rules layer remains the only authority on *what is legally available*, and the IOE remains the only authority on *how a hypothetical is expressed as engine input*.

---

## D. Greedy-portfolio limitations

### D.1 The defect

Revision 2 correctly declined to claim mathematical optimality, but never stated **what greedy actually loses** — and one of its rules was actively wrong: `if trial_tax > prev_tax: exclude`. That myopic test **rejects candidates that are beneficial only in combination**, which is exactly the case near credit ceilings, phase-out thresholds, and bracket boundaries.

### D.2 Stated limitations (normative — must be reflected in UI and API)

| # | Limitation | Consequence |
|---|---|---|
| L-1 | **Order dependence** — assembly follows rank order, which is not the optimal order | A different order could yield a better portfolio |
| L-2 | **No backtracking** — an accepted candidate is never reconsidered | The result is a local optimum |
| L-3 | **Myopic acceptance** — decided on immediate Δobjective | Jointly-beneficial combinations can be missed (addressed by D.3) |
| L-4 | **No approximation guarantee** — with shared budgets this is knapsack-like; greedy has no bounded-ratio guarantee here | Gap to optimum is unbounded in the worst case |
| L-5 | **Substitute branch selection** — the ranked branch wins | The unranked branch may have been better under the objective |
| L-6 | **Bounded exploration** — only a fraction of the feasible space is examined | Unexplored alternatives exist by construction |

### D.3 Mitigations (deterministic and bounded)

**Deferred reconsideration (fixes L-3).** A candidate failing the acceptance test is no longer discarded. It moves to a `deferred_pending_combination` set and is re-tested after each subsequent acceptance, because a later candidate may change its value (ceiling headroom, threshold crossing).

```text
portfolio_membership += deferred_pending_combination     # extends the v2 §5.1 enum
```

**Bounded local improvement (mitigates L-1, L-2, L-5).** After the greedy pass, an optional improvement phase runs deterministic single-swap and pairwise-exchange moves (swap a selected candidate for a deferred/excluded-by-constraint one), accepting a move only if `Δobjective ≥ epsilon_accept`. It is:
- **deterministic** — moves are enumerated in a fixed canonical order, first-improvement accepted, no randomness;
- **bounded** — capped by `max_improvement_engine_runs` (default 30), so the §27 latency budget holds;
- **terminating** — each accepted move strictly increases the objective by ≥ epsilon, so the phase cannot cycle.

**Honest labelling (addresses L-4, L-6).** Recorded on `ioe.strategy_portfolio` and returned by the API:

```text
assembly_method:   greedy_ranked | greedy_ranked_with_local_improvement
optimality_claim:  none | locally_improved        # the value 'optimal' does not exist
```
plus `deferred_count`, `excluded_count`, `improvement_moves_applied`, `improvement_runs_used`, and `unexplored_alternatives_count`.

No surface — API, UI copy, or documentation — may describe the portfolio as optimal, best, or maximal. Permitted phrasing: *"a compatible strategy set assembled from your highest-ranked opportunities."*

### D.4 Engine-run budget (updates v2 §12.5 / §27)

`1 baseline + N_evaluable standalone + N_selected incremental + D deferred re-tests + ≤30 improvement + 1 combined`. Deferred re-tests are capped at `max_deferred_retests` (default 40). Worst case at the 50-candidate cap: ≈ 172 pure in-process runs. Since the arithmetic is `Decimal` in-process against a warm rule cache, the §27 targets are retained; if measured p95 exceeds budget, the improvement phase is the first thing disabled (it is optional by construction), and that fallback is itself recorded in `assembly_method`.

> **D-15:** enable the improvement phase by default (recommended) or ship greedy-only in MVP.

---

## E. Interaction-delta mathematics

### E.1 The defect

Revision 2 defined `interaction_delta = sum_of_standalone − portfolio_total` and `additivity_verified = abs(interaction_delta) <= 0.01` — with a **float literal**, no sign convention, no per-candidate attribution, and no statement of which quantity is safe to display.

### E.2 Definitions and invariants

All quantities are `Decimal` at money scale 2 (`ROUND_HALF_UP`, §9.1). `epsilon_money = Decimal('0.01')`.

```text
standalone_i              = baseline_tax − tax(baseline ⊕ c_i)                # one engine run each
incremental_i             = tax(P_{k-1}) − tax(P_k)                           # at apply_order k
portfolio_total_benefit   = baseline_tax − tax(P_n)                           # combined engine run
```

**Invariant I-1 — telescoping (exact).**
```
Σ_i incremental_i  ==  portfolio_total_benefit          (to the cent, no tolerance)
```
This holds by construction because the increments telescope: `Σ (tax(P_{k-1}) − tax(P_k)) = tax(P_0) − tax(P_n)`.

**Invariant I-2 — final-run reconciliation.**
```
tax(P_n) from the combined run  ==  tax(P_n) from the last accepted trial run
```
This guards against non-idempotent or order-sensitive lever application. A violation is a **hard error** that fails the run (`error_code=PORTFOLIO_RECONCILIATION_FAILED`) rather than a silently reported figure. I-1 and I-2 are both asserted in code and covered by tests.

**Interaction quantities.**
```text
interaction_delta_total  = Σ_i standalone_i − portfolio_total_benefit
interaction_delta_i      = standalone_i − incremental_i           # per-candidate
```
Consistency: `Σ_i interaction_delta_i == interaction_delta_total` (follows from I-1).

**Sign convention (explicit).**

| Condition | Classification | Meaning |
|---|---|---|
| `interaction_delta_total > epsilon_money` | `sub_additive` | Overlap — shared pools, credit ceilings, phase-outs. Summing cards would **overstate** the benefit |
| `abs(interaction_delta_total) ≤ epsilon_money` | `additive` | Strategies are independent at this margin |
| `interaction_delta_total < −epsilon_money` | `super_additive` | Synergy — combined benefit exceeds the sum of parts |

```text
additivity_class: additive | sub_additive | super_additive
additivity_verified: bool     # true only when additivity_class == 'additive'
```

### E.3 Attribution and what may be displayed

`incremental_i` is **path-dependent**: it depends on `apply_order`, so it is exact and sums correctly (I-1) but is not an order-independent "fair share." An order-independent attribution is the Shapley value, which requires evaluating all `2^n` subsets — infeasible beyond very small `n`.

**MVP rule (D-14):**
- **Displayed per card:** `incremental_portfolio_benefit`, labelled *"additional benefit when combined with your other selected strategies."* It is the only per-item number that sums to the headline total.
- **Displayed as context:** `standalone_potential`, labelled *"benefit if this were your only strategy."*
- **Never displayed:** `Σ standalone_i` as a total, in any surface.
- **Recorded:** `attribution_method = incremental_path_dependent`, `attribution_method_version`, and `apply_order`, so the path is auditable.

Exact Shapley attribution is deferred behind the same field (`attribution_method = shapley_exact`) and may be enabled for small portfolios (`n ≤ 5`, ≤32 subset evaluations) in a later release. It is not claimed now.

When `additivity_class = sub_additive`, the API returns a structured `overlap_explanation` derived from the §16 relationship edges (which `shared_resource_code` or ceiling caused it) — so the gap between standalone figures and the total is explained, not merely disclosed.

---

## F. Migration ordering

### F.1 The defect

Revision 2 §30 listed `0026_ioe` (the `ioe` schema) before `0027_ioe_contract` (rules/tax_kb/reco), while §31 required **P0 = contract first**. The two contradicted. Worse, `reco.recommendation.optimization_run_id` is an FK to `ioe.optimization_run`, so it **cannot** ship in the same revision as the contract changes unless `ioe` already exists. Revision 2 also numbered the seed `96_seed_ioe.sql`; verified against the repository, the last seed is `94_seed_tkms.sql`, so the next is **95**.

### F.2 Corrected ordering (dependency-driven)

Verified current heads: last SQL `19_tkms.sql` / `94_seed_tkms.sql`; last Alembic revision `0025_seed_tkms`.

| Order | Revision | SQL file | Contents | Depends on | Phase |
|---|---|---|---|---|---|
| 1 | `0026_rules_contract` | `20_rules_contract.sql` | Contract-v2 additive tables (`rules.rule_action`, `rule_required_document`, `rule_dependency`, `rule_deadline`, `rule_shared_resource`), additive columns on `rules.rule_outcome` (`economic_effect_type`, `reversibility`, `portfolio_lever_code`), `tax_kb.tax_rule_version.eligibility_basis_codes jsonb` | `0025_seed_tkms` | **P0** |
| 2 | `0027_ioe` | `21_ioe.sql` | `ioe` schema: all v2 §7 tables **plus** `rule_snapshot`, `rule_snapshot_artifact`, `run_rule_snapshot` (§A.2); CHECKs, RLS + FORCE, immutability/transition triggers, partial unique indexes | `0026` | **P1** |
| 3 | `0028_reco_ioe_link` | `22_reco_ioe_link.sql` | `reco.recommendation` additive: `optimization_run_id` FK → `ioe.optimization_run`, `calculation_basis`, `evidence_status`; widened lifecycle CHECK | **`0027`** (FK target must exist) | **P1** |
| 4 | `0029_seed_ioe` | `95_seed_ioe.sql` | Default `weight_config` v1, lever registry, relationship registry, assumption registry | `0027` | pre-**P2** |

**Downgrade order is strict reverse:** `0029 → 0028 → 0027 → 0026`. Each drops only what it created; no existing data is modified.

### F.3 Lock-safety and online-migration notes

| Operation | Technique | Why |
|---|---|---|
| `ADD COLUMN ... NULL` (no default) | plain | Metadata-only in PostgreSQL 11+; no table rewrite |
| New FK `optimization_run_id` | `ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT` in a separate statement | Avoids a long `ACCESS EXCLUSIVE` lock while scanning `reco.recommendation` |
| Widened lifecycle CHECK | Drop old CHECK, add new one `NOT VALID`, then `VALIDATE` | New CHECK is a **superset**: it retains legacy values (`generated`, `viewed`, `accepted`, `rejected`, `completed`) **and** adds the 8 §5.1 states, so existing rows validate and no backfill is required |
| Lifecycle value migration | **Dual-accept, no rewrite.** `generated` is read as `new` at the application boundary; historical rows are never rewritten | Preserves immutable history |
| New tables/triggers | plain | New objects only |

**Ordering constraint on application deploy:** the widened CHECK (revision `0028`) must be applied **before** any application build that writes the new lifecycle states. Standard expand-then-migrate: `0026–0029` deploy first, application second.

### F.4 Phase alignment (amends v2 §31)

| Phase | Requires |
|---|---|
| **P0** contract emission | `0026` only — `RulesEvaluatorService` emits contract v2 with fields nullable; no `ioe` schema needed |
| **P1** schema + models | `0027`, then `0028` |
| **P2** pure domain | `0029` (registries) present for fixture-backed tests |
| **P3–P6** | unchanged from v2 §31 |

---

## Blocking decisions (this revision)

New decisions raised by these amendments. Prior decisions **D-1 – D-9** (Revision 2 §32) stand and are unchanged.

| # | Decision | Recommended | Alternative | Blocking? |
|---|---|---|---|---|
| **D-10** | Rule-snapshot depth | **Hybrid:** content hashes for all artifacts + materialized `content jsonb` for the small high-risk set (formula expressions, calc constants, engine reference dataset) — bounded storage, replayable after drift | Hashes only (drift detectable, replay impossible) | **Yes** |
| **D-11** | Add `REFERENCE_DATA_VERSION` + content hashing to `core/data.py` | Approve — additive, no calculation change, closes the pinning hole | Leave unpinned (reference-data drift stays undetectable) | **Yes** |
| **D-12** | Immutability guard on published-rule child artifacts (additive TKMS triggers on `calc_formula`, `rule_condition*`, `rule_outcome` once referenced by a published version) | Approve as defence in depth alongside snapshots | Rely on drift detection alone | No |
| **D-13** | Primary portfolio objective | `current_year_tax_reduction_net_of_expenditure`, deferral discounted per D-5 | Gross reduction, or multi-horizon comparable value | **Yes** |
| **D-14** | Interaction attribution method | `incremental_path_dependent` for MVP (exact, sums to total, labelled); Shapley deferred | Shapley for n ≤ 5 now (32 extra engine runs) | No |
| **D-15** | Local improvement phase | Enabled, capped at 30 engine runs | Greedy-only MVP | No |
| **D-16** | Contract field change `portfolio_application` → `portfolio_lever_ref` | Approve — restores the rules/IOE boundary | Keep rules-supplied field mappings (unsafe, dual authority) | **Yes** |

## Migration-impact summary (delta to Revision 2)

| Change | Impact |
|---|---|
| Revision **renumbering** | v2's `0026_ioe`/`0027_ioe_contract`/`0028_seed` → `0026_rules_contract`/`0027_ioe`/`0028_reco_ioe_link`/`0029_seed_ioe`. **Four** revisions, not three |
| SQL file renumbering | `20_rules_contract.sql`, `21_ioe.sql`, `22_reco_ioe_link.sql`, `95_seed_ioe.sql` (v2's `96_` was wrong — `94` is the current last seed) |
| New `ioe` tables | +3 (`rule_snapshot`, `rule_snapshot_artifact`, `run_rule_snapshot`) → 23 total |
| `reco.recommendation` changes | Split out of the contract revision into `0028` (FK requires `ioe` to exist first) |
| Engine module | **New:** additive `REFERENCE_DATA_VERSION` constant in `core/data.py` (D-11). First change to an approved engine file in this program — additive only, zero calculation impact |
| TKMS validation | **New:** additive publish-time check that referenced `lever_code`s exist (§C, enforcement 4). No schema change |
| Destructive changes | **Still none.** No drops, retypes, or data rewrites |
| Lock profile | FK and CHECK added `NOT VALID` then validated separately; no long exclusive locks |

## Risk register (delta)

Revision 2 risks R-1 – R-13 stand. Amendments:

| # | Risk | Sev | Change |
|---|---|---|---|
| R-5 | Determinism breaks via unstable serialization | High | **Reduced** — rule snapshots + `engine_reference_data_hash` close the content-drift hole |
| R-14 | **Reference-data drift undetectable if D-11 declined** | **High** | New. Mitigation: approve D-11; otherwise accept that replay verification is incomplete and label `replay_status=unavailable` for reference-data-dependent runs |
| R-15 | **Greedy assembly materially below optimum** | Medium | New. Mitigation: deferred reconsideration + bounded improvement (§D.3); `optimality_claim` never asserts optimality; solver seam defined |
| R-16 | **Snapshot storage growth** | Low | New. Mitigation: content-addressed sharing (`snapshot_hash` UNIQUE) bounds rows by distinct rule states; materialized content limited to the D-10 high-risk set |
| R-17 | **Objective/display mismatch** — objective governs selection while a different figure is displayed | Medium | New. Mitigation: `objective_metric` returned with every portfolio; headline remains the engine-verified `portfolio_total_benefit`; documented in §B.3 |
| R-18 | **Migration/deploy ordering violated** (app writing new lifecycle states before `0028`) | Medium | New. Mitigation: expand-then-migrate discipline (§F.3); superset CHECK means an early app deploy still validates |

## Ready for implementation only when

Revision 2's checklist stands, plus:

- [ ] **D-10, D-11, D-13, D-16** approved (the blocking items of this revision).
- [ ] D-12, D-14, D-15 answered or accepted as recommended.
- [ ] The rule-snapshot artifact kind list (§A.2) is confirmed complete against everything the engine and rules evaluator actually read.
- [ ] `replay_status` semantics (§A.3) — including the honest "detectable but not re-computable" limitation — are accepted.
- [ ] The objective function and cost semantics (§B.2), especially **contribution ≠ expenditure**, are accepted.
- [ ] Greedy limitations L-1 – L-6 (§D.2) are accepted as product constraints, and the prohibition on "optimal" phrasing is accepted for UI copy.
- [ ] Invariants **I-1** (telescoping) and **I-2** (final-run reconciliation) are accepted as hard, run-failing assertions.
- [ ] The display rule — `incremental` per card, `standalone` as context, **`Σ standalone` never shown as a total** — is accepted.
- [ ] The four-revision migration order and expand-then-migrate deploy sequence (§F) are accepted.

---

## Implementation gate

No implementation code will be written until this revision is approved. On approval I will begin with **Phase P0 / revision `0026_rules_contract`**, in the order fixed by §F.

> **Please approve Revision 2.1, or adjust its decisions — with particular attention to the four blocking items: D-10 (snapshot depth), D-11 (engine reference-data version — the first additive change to an approved engine file), D-13 (primary objective metric), and D-16 (lever-binding authority). Reply "approved" (noting any adjustments) and I will start Phase P0.**
