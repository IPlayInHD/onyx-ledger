# IOE Phase P2 — Verification Record

**Scope:** the pure domain (`backend/app/services/ioe/domain/`).
**Status:** P2 approved subject to the confidence correction recorded in §3, which is implemented here.
**Companion documents:** `ioe-architecture-v2.md`, `ioe-architecture-v2.1.md`.

This record states the portfolio invariants exactly, confirms canonicalization coverage, and documents the revised support-score model, each with the test that verifies it.

---

## 1. Shared conventions for all invariants

I-1 and I-2 are stated over one set of conventions. Where an invariant mentions a value, that value is produced under these rules and no others.

| Convention | Definition |
|---|---|
| **Objective** | The single `objective_metric` pinned on the portfolio for that assembly run (default `current_year_tax_reduction_net_of_expenditure`). Both invariants are evaluated against the **same** objective within a run; they are never compared across runs with different objectives. |
| **Tax function** | `evaluate(inputs) -> Decimal` — the injected tax engine, returning **total payable**. The IOE never computes tax itself. |
| **Benefit sign convention** | A benefit is a **reduction in tax payable**: `benefit = tax_before − tax_after`. Positive benefit means less tax. |
| **Interaction sign convention** | `interaction_delta = sum_of_standalone − portfolio_total_benefit`. **Positive** ⇒ sub-additive (summing the cards would **overstate**). **Negative** ⇒ super-additive (synergy; summing would **understate**). |
| **Decimal policy** | All money is `Decimal`, scale 2, `ROUND_HALF_UP`. Floats never appear. Comparisons use `Decimal`, never float literals. |
| **Rounding stage** | Quantization to scale 2 happens **once**, when a value is finalized on the `StrategyPortfolio` / `PortfolioMember`. Intermediate arithmetic is unrounded, so rounding is applied at one stage only and never compounded. |
| **Tolerance** | `EPSILON_ACCEPT = Decimal("0.01")` — one cent, the smallest representable money unit. Used for acceptance, reconciliation, and additivity classification alike. |

---

## 2. Portfolio invariants

### I-1 — Telescoping

> **Statement.** For a portfolio with selected members `m₁…mₙ` in `apply_order`, where `Pₖ` is the input state after applying the first `k` members and `tax(Pₖ)` is the engine's total payable for that state:
>
> ```
> incremental_k        = tax(P_{k−1}) − tax(P_k)
> portfolio_total_benefit = tax(P₀) − tax(P_n)          # P₀ = the cloned baseline
>
> I-1:   Σ_{k=1..n} incremental_k  ==  portfolio_total_benefit
> ```
>
> Equality is **exact at money scale 2** — no tolerance is permitted, because the identity is structural: the sum telescopes.

**Why it matters.** I-1 is what makes the per-card number safe to display. `incremental_portfolio_benefit` is the only per-item figure that sums to the headline; `standalone_potential` does not and must never be totalled.

**Holds regardless of** the sign of `interaction_delta`, the number of members, or whether any candidate was deferred or excluded.

**Implementation:** `portfolio.verify_telescoping()`; increments are assigned in `assemble()` from the tracked `running_tax`.

**Tests:**
- `test_I1_telescoping_holds_exactly_for_every_shape` — 1, 2, and 4-member portfolios; asserts exact equality *and* the helper.
- `test_total_comes_from_the_combined_engine_run_and_telescopes`
- `test_super_additive_synergy_is_permitted_not_treated_as_an_error` — I-1 still holds when the interaction is negative.
- `test_per_candidate_deltas_sum_to_the_aggregate_delta` — the corollary `Σ interaction_delta_k == interaction_delta_total`.

### I-2 — Combined-run reconciliation

> **Statement.** Let `tax_trial` be the engine result for the last accepted trial state, and `tax_final` the result of the **final combined run** over the same accumulated input state. Then:
>
> ```
> I-2:   | tax_trial − tax_final |  ≤  EPSILON_ACCEPT      (Decimal("0.01"))
> ```
>
> A violation raises `PortfolioReconciliationError` and **fails the run**. It is never reported as a discrepancy and never surfaced as a total.

**Why it matters.** The headline total is defined as `baseline_tax − tax_final`. If the combined run cannot reproduce the state the increments were measured against — for example because lever application is order-dependent or not idempotent — then the total is unprovable, and an unprovable total must not reach a user.

**Tolerance rationale.** One cent, matching the money scale: the two runs evaluate the *same* input state, so any difference beyond representation error indicates a real defect.

**Implementation:** the reconciliation check at the end of `portfolio.assemble()`.

**Tests:**
- `test_I2_reconciliation_holds_when_levers_are_order_independent` — the normal path, asserting `portfolio_tax == baseline_tax − portfolio_total_benefit`.
- `test_reconciliation_failure_is_a_hard_error` — an engine whose final run disagrees with the accepted trial raises rather than returning a number.

### Explicitly NOT an invariant

> **`portfolio_total_benefit ≤ sum_of_standalone` is NOT asserted anywhere, and must not be.**

Strategies can be **super-additive**: applying two together may yield more than the sum of each alone (e.g. one deduction pushing income below a phase-out threshold that makes another more valuable). Imposing the inequality would flag genuine synergy as a defect and would bias the system toward under-reporting benefit.

`interaction_delta` is therefore **signed**, and `additivity_class` has three values, not two.

**Tests:**
- `test_super_additive_synergy_is_permitted_not_treated_as_an_error`
- `test_interaction_sign_convention_is_consistent_in_both_directions`
- `test_classification_thresholds_use_decimal_epsilon`
- `test_invariants_share_one_objective_and_rounding_stage` — confirms both invariants hold under a single objective, Decimal policy, and rounding stage.

---

## 3. Support-score model (revised)

The blanket proportional ceiling from the first P2 submission is **withdrawn**. Multiplying every assumption-bearing score by 0.80 penalized weak results as much as strong ones and changed what the score means. It was not an approved scoring policy.

### 3.1 Three preserved values

| Field | Meaning |
|---|---|
| `raw_support_score` | Weighted sum of the support components, **before** any assumption adjustment. 0–100. |
| `assumption_adjusted_score` | `raw` reduced by uncertainty derived from the assumptions themselves. 0–100. **Uncapped.** |
| `display_support_score` | `min(assumption_adjusted_score, 80)` — the user-facing value. |
| `cap_applied` | `True` only when the cap actually bound (`adjusted > 80`). |
| `cap_reason_code` | `ASSUMPTION_BEARING_RESULT` when capped, otherwise `null`. |

The cap is a **`min()` on the displayed value only**. A result already below the cap is left exactly as computed.

### 3.2 Uncertainty inputs

The adjustment is computed per assumption from **materiality, source, certainty, supporting evidence, eligibility impact, and measured sensitivity** — and explicitly **not** from the number of assumptions. The strongest single penalty governs, so one high-materiality, eligibility-affecting, highly-sensitive assumption outweighs many immaterial ones.

Each contribution is recorded as an `UncertaintyComponent` (code, materiality, source, eligibility impact, sensitivity, penalty, reason code) so the adjustment is independently auditable and is not folded into the weighted support sum.

`scenario_uncertainty` is **no longer a weighted support component** — it is the adjustment stage. Including it in both would double-count the same doubt.

### 3.3 Ordering

Ranking uses `assumption_adjusted_score` as the **secondary ordering key**, immediately after the overall recommendation score. Two results tied at the displayed cap still order deterministically by their uncapped support, without the displayed number taking on a meaning it does not have.

### 3.4 User-facing wording

`SUPPORT_SCORE_DISCLAIMER` accompanies the value wherever it is shown:

> This is a support score: it reflects how well this result is backed by the information supplied, its documentation, and the published rule it relies on. It is not a probability that the CRA will accept a claim, and not a probability of receiving the amount shown.

**Tests:** `test_assumption_bearing_results_are_capped_for_display_only`, `test_low_support_assumption_result_is_not_further_penalized_by_the_cap`, `test_uncertainty_uses_source_and_certainty_not_just_materiality`, `test_measured_sensitivity_softens_the_penalty`, `test_weak_evidence_amplifies_assumption_uncertainty`, `test_confidence_is_not_reduced_by_assumption_COUNT`, `test_eligibility_affecting_assumption_costs_more_than_amount_only`, `test_uncertainty_components_are_recorded_for_audit`, `test_support_score_is_labelled_as_support_not_probability`, `test_ranking_breaks_cap_ties_using_the_adjusted_score`.

---

## 4. Canonicalization coverage confirmation

| # | Concern | Rule | Test |
|---|---|---|---|
| 1 | **Unicode normalization** | All strings **and object keys** are NFC-normalized before serialization. Keys colliding under NFC are **rejected**, not silently merged. | `test_unicode_normalization_nfc`, `test_unicode_normalization_applies_to_keys_too`, `test_keys_colliding_under_normalization_are_rejected_not_silently_merged` |
| 2 | **Negative zero** | `-0`, `-0.00`, and values rounding to zero all normalize to `+0` at every scale. | `test_negative_zero_is_normalized_across_all_scale_helpers`, `test_negative_zero_normalizes` |
| 3 | **NaN / Infinity rejection** | `Decimal("NaN")`, `sNaN`, `±Infinity` raise. Floats (including `float('nan')`) are rejected everywhere. | `test_nan_and_infinity_are_rejected`, `test_float_is_rejected_everywhere` |
| 4 | **Maximum Decimal precision** | Rejected above `MAX_SIGNIFICANT_DIGITS = 38`. Magnitude bounds additionally mirror the DB domains (`money ≤ 999999999999.99` for `NUMERIC(14,2)`; `rate ≤ 999.999999` for `NUMERIC(9,6)`), so an unstorable value cannot reach a hash. | `test_maximum_decimal_precision_is_bounded`, `test_magnitude_bounds_match_the_database_domains` |
| 5 | **Dictionary-key restrictions** | Keys must be text (`str` or a string-valued enum). Integers, tuples, and other types raise. | `test_dictionary_key_restrictions` |
| 6 | **Date handling** | `date` → ISO-8601. `datetime` rejected (wall-clock excluded from hashes). `time` rejected as an unknown type. | `test_date_handling_covers_date_reject_datetime_and_reject_time`, `test_dates_serialize_iso_and_datetimes_are_rejected` |
| 7 | **Explicit collection sort keys** | Arrays preserve caller order (ordering is semantic). Collections destined for a hash use an explicit key: rule-version sets by string, snapshot artifacts by `(artifact_kind, artifact_key)`, assumptions by `code`. `set`/`frozenset` are rejected. | `test_explicit_collection_sort_keys`, `test_array_order_is_preserved_not_sorted`, `test_sets_are_rejected_because_their_order_is_not_stable`, `test_spec_hash_ignores_rule_version_set_ordering` |
| 8 | **Domain-separated hashes** | Every artifact type hashes under its own tag `onyx.ioe.<domain>.v<serialization_version>`. Nine domains: optimization spec/result, scenario spec/result, rule snapshot, assumption set, portfolio result, version manifest, weight config. Identical payloads across domains produce different digests; unknown domains raise; the tag pins the serialization version so a rules change invalidates stored digests rather than silently reinterpreting them. | `test_hashes_are_domain_separated_per_artifact_type`, `test_unknown_hash_domain_is_rejected`, `test_domain_tag_pins_the_serialization_version`, `test_spec_and_result_hashes_of_the_same_payload_differ` |

**Additional determinism guarantees already accepted:** spec hashes never include results; result hashes always include their spec hash; a scenario spec hashes its baseline snapshot and pinned versions rather than deltas alone; scenario lever order is significant; and digests are stable across process restarts and `PYTHONHASHSEED` values (`test_hash_is_stable_across_processes_and_hash_seeds`, verified in subprocesses).

`CANONICAL_SERIALIZATION_VERSION` is raised to **1.1.0** for this change. Because the version is embedded in every domain tag, digests produced under 1.0.0 do not verify under 1.1.0 — which is the intended behaviour, since the rules changed.

---

## 5. Verification summary

| Item | Result |
|---|---|
| IOE domain unit tests | **140 passed** |
| Full backend suite | **258 passed** |
| Lint (`ruff`) | clean |
| Schema `autogenerate` | clean (no P2 schema change) |
| Hash stability across processes / hash seeds | verified |

**Carried into P3:** the confidence fields (`raw_support_score`, `assumption_adjusted_score`, `display_support_score`, `cap_applied`, `cap_reason_code`) must be persisted on `ioe.optimization_candidate` and surfaced by the API alongside `SUPPORT_SCORE_DISCLAIMER`. This requires an additive column change in the P3 migration, since P1 stored a single `confidence_score`.
