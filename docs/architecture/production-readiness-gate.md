# Onyx Ledger — Production Readiness Gate

**Commit** `4593441` (+ security fix `0036_partition_rls`) · **Branch** `claude/zen-hypatia-8cxiza`
**Suite** 499 passed, 0 skipped · **Environment** single-container PostgreSQL 16 + pgvector, Python 3.11

Every PASS below names concrete evidence. Items with no implementation are FAIL,
not "architecturally intended". Three findings were fixed during the gate and are
marked as such; one of them was an exploitable cross-tenant read.

---

## 1. Build and repository integrity

| # | Item | Result | Evidence |
|---|---|---|---|
| 1.1 | Clean checkout builds without local-only files | **PASS** | Container reset mid-gate wiped the working tree; `git reset --hard origin/…` + `python3 -m venv .venv && pip install -e '.[dev]'` reproduced a working build twice |
| 1.2 | Dependency locks current and reproducible | **FAIL** | `pyproject.toml` declares only `>=` ranges. No `poetry.lock`, `uv.lock`, `requirements.txt`, or hashes. Two installs a week apart can resolve different versions |
| 1.3 | Full test suite passes from a clean environment | **PASS** | `./scripts/run_backend_tests.sh` → `499 passed`, drops and recreates `onyx_test` each run |
| 1.4 | Ruff passes | **PASS** | `ruff check app workers tests` → `All checks passed!` |
| 1.5 | Type checking passes | **FAIL** | `mypy app` → **67 errors in 22 files**. CI runs `mypy app \|\| true` — advisory only, so this never blocks |
| 1.6 | Formatting passes | **FAIL** | `ruff format --check` → **149 of 218 files would be reformatted**. `ruff format` was never adopted; CI does not run it |
| 1.7 | Import-boundary checks | **NOT APPLICABLE** | No import-linter or equivalent configured. Layering is enforced by review and by `test_routes_contain_no_calculation_or_reconstruction` (AST-based), not by a general tool |
| 1.8 | Alembic autogenerate clean | **PASS** | `alembic check` → `No new upgrade operations detected` |
| 1.9 | SQL-authoritative migrations match Alembic revisions | **PASS** | 36 revisions ↔ 36 SQL files; each revision body is a single `apply_sql_file(...)` naming its file |
| 1.10 | Upgrade base → head succeeds | **PASS** | Fresh `gate_alembic` database: `ALEMBIC base->head: OK` |
| 1.11 | Downgrade and re-upgrade succeed | **PASS (with caveat)** | `head->base: OK`, `re-upgrade: OK`. Caveat in §2 — downgrade works only because revision `0001_foundation` drops every schema |
| 1.12 | Seed migrations idempotent | **PASS** | `db/sql/9*.sql` re-applied to an already-seeded database with `ON_ERROR_STOP=1` → `SEEDS: idempotent on re-apply` |
| 1.13 | No committed secrets/credentials/local paths | **PASS** | Regex scan over `app/ workers/ db/ deploy/` for assigned high-entropy literals → no matches. Test JWT secret is supplied by env in `scripts/run_backend_tests.sh` |
| 1.14 | No committed test financial data | **PASS** | Seeds contain reference data (brackets, credits, jurisdictions) only; user financial rows are created per test |
| 1.15 | Git status clean after checks | **PASS** | `git status --porcelain` empty at HEAD |

**Fixed during gate:** two tests hardcoded `postgresql://onyx_migrator@localhost:5432/onyx`, a
database the harness never provisions. They passed only on a machine where someone had created
it by hand and failed on a clean checkout. Both now derive the DSN from `ONYX_DATABASE_URL`
(`tests/conftest.py::owner_dsn`). This is why 1.3 now reports 0 skips rather than 1.

---

## 2. Migration and rollback readiness

### The rollback finding

**Every IOE migration has a no-op `downgrade()`.** All ten bodies read:

```python
def downgrade() -> None:
    # The SQL baseline is torn down as a unit at revision 0001_foundation.
    pass
```

`alembic downgrade base` succeeds only because `0001_foundation.downgrade()` calls
`drop_all_schemas()`. There is **no per-revision rollback anywhere in the project**. Rolling
back one migration in production is not possible; the only supported reverse operation destroys
the entire database.

**Therefore every migration listed below is FORWARD-ONLY in production.** Recovery from a bad
migration is restore-from-backup, and §13 shows no verified backup exists.

### Per-migration detail

| Revision | Purpose | Tables / columns | Lock & runtime risk | Expected time | Rollback | Safe after writes? | Ordering |
|---|---|---|---|---|---|---|---|
| `0026_rules_contract` | Contract-v2 rule metadata | +5 tables in `rules`; +4 cols `rules.rule_outcome`; +1 col `tax_kb.tax_rule_version` | `ADD COLUMN` nullable — metadata-only, brief `ACCESS EXCLUSIVE`. 7 DDL ops | < 1 s | none | n/a | after `0025` |
| `0027_ioe` | IOE schema | 23 new tables, triggers, RLS, 18 DDL ops | All CREATE — no existing-table locks | < 2 s | none | n/a | after `0026` |
| `0028_reco_ioe_link` | Link reco → IOE | +3 cols `reco.recommendation`; CHECK widened | `ADD COLUMN` nullable + CHECK `NOT VALID` then `VALIDATE` (`SHARE UPDATE EXCLUSIVE`, allows reads/writes) | < 1 s at current volume; VALIDATE scans the table | none | n/a | after `0027` |
| `0029_ioe_support_scores` | 5-stage support score | +5 cols, 6 CHECKs, trigger on `ioe.optimization_candidate` | Same NOT VALID/VALIDATE pattern | < 1 s | none | n/a | after `0028` |
| `0030_ioe_portfolio` | Portfolio persistence | +10 cols `strategy_portfolio`; +2 tables; CHECK widening | Drops and recreates a CHECK on `candidate_cost` — brief `ACCESS EXCLUSIVE` | < 1 s | none | n/a | after `0029` |
| `0031_rules_cost_taxonomy` | Separated cost taxonomy | CHECK widened on `rules.rule_action` | Constraint drop/recreate | < 1 s | none | **Yes** — purely widening; old values stay valid | after `0030` |
| `0032_ioe_result_rls` | RLS on IOE child evidence | RLS + policies on 19 tables | `ALTER TABLE … ENABLE/FORCE RLS` takes `ACCESS EXCLUSIVE` briefly per table | < 1 s | none | **No** — reverting reopens a cross-tenant read | after `0027` |
| `0033_ioe_cost_and_reeval` | Cost provenance + re-eval state | +3 cols `candidate_cost`; +2 cols `optimization_candidate`; membership CHECK widened | ADD COLUMN + CHECK | < 1 s | none | Partly — `requires_re_evaluation` rows become invalid under the old CHECK | after `0032` |
| `0034_ioe_scenarios` | Scenario lifecycle | +16 cols `ioe.scenario`; +3 tables; 6 CHECKs; trigger replaced; RLS | 19 DDL ops; replaces `trg_guard_transition` | < 2 s | none | **No** — sealed-column guard would be lost | after `0033` |
| `0035_ioe_outbox_and_projection` | Outbox + governed projections | +1 table + audit table; +4 cols `rule_outcome`; +3 cols `multi_year_projection`; 4 SECURITY DEFINER functions; new role | 6 DDL ops on existing tables; `CREATE ROLE` | < 1 s | none | **No** — in-flight outbox events would be orphaned | after `0034` |
| `0036_partition_rls` *(gate fix)* | RLS on partitions | RLS + policy on 6 partitions; event trigger | Per-partition `ACCESS EXCLUSIVE`, brief | < 1 s | none | **No** — reverting reopens the cross-tenant read | after `0035`; must precede any traffic |

### Smoke test results

| Scenario | Result | Evidence |
|---|---|---|
| Empty database | **PASS** | `gate_empty`: full `apply_schema.sh` → `EMPTY-DB: schema applies OK` |
| Alembic base→head→base→head | **PASS** | `gate_alembic`: all three directions OK |
| Representative populated database | **PASS** | `onyx_test` carries ~100 income rows, ~40 users, scenarios, runs, portfolios from the suite; migrations re-applied over it |
| Legacy contract-v1 rules present | **PASS** | Inserted `GATE_V1_LEGACY` with `eligibility_basis_codes IS NULL`; migrations apply; §4 confirms it normalizes to `indeterminate` |
| Active scenarios and optimization runs | **PASS** | 499-test suite runs against a database populated by earlier tests in the same run |

**Index creation is not `CONCURRENTLY`.** `CREATE INDEX` appears 30+ times across IOE SQL without
`CONCURRENTLY`, taking a write lock for the duration. Negligible on empty tables, a write outage
on populated ones. Only `10_ai.sql` and `17_indexes.sql` use `CONCURRENTLY`.

---

## 3. Determinism and replay

| # | Item | Result | Evidence |
|---|---|---|---|
| 3.1 | Canonical serialization version pinned | **PASS** | `CANONICAL_SERIALIZATION_VERSION = "1.1.0"`, embedded in every domain separator |
| 3.2 | Hashes domain-separated | **PASS** | 9 domains; `domain_hash()` rejects unknown domains — `test_unknown_hash_domain_is_rejected` |
| 3.3 | Unicode normalization + collision rejection | **PASS** | `test_keys_colliding_under_normalization_are_rejected_not_silently_merged` |
| 3.4 | Semantic-specific Decimal scales/bounds | **PASS** | `money()` NUMERIC(14,2), `rate()` NUMERIC(9,6), `quantity()` 6dp; `test_canonical.py` |
| 3.5 | Floats/datetimes/sets/NaN/Infinity rejected | **PASS** | `test_float_is_rejected_everywhere`, `test_sets_are_rejected…`, `test_nan_and_infinity_are_rejected`, `test_dates_serialize_iso_and_datetimes_are_rejected` |
| 3.6 | Optimization spec + result hashes replay | **PASS** | `test_optimization_spec_hash_replays_from_stored_manifest` — rebuilds from stored `version_manifest` + `run_rule_version` rows |
| 3.7 | Scenario spec + result hashes replay | **PASS** | `test_scenario_spec_and_result_hashes_replay_from_stored_rows` — spec rebuilt from `scenario_lever`/`scenario_assumption` rows |
| 3.8 | Portfolio result hashes replay | **PARTIAL** | `test_portfolio_reconciliation_replays_from_stored_rows` re-derives I-1/I-2 from stored rows. There is **no `portfolio_result_hash` populated** — the column exists but is never written, so there is no portfolio hash to replay |
| 3.9 | Rule snapshots constrain replay | **PASS** | `test_rule_published_after_pinning_does_not_enter_the_in_flight_run`; `PinnedEligibilityRechecker` holds no session |
| 3.10 | All versions pinned | **PASS** | Manifest carries 16 versions: engine, reference data, contract, normalization, scoring, confidence, relationship, canonical, portfolio service/assembly, lever registry, cost taxonomy, eligibility recheck, objective code/version |
| 3.11 | Stable across restarts and `PYTHONHASHSEED` | **PASS** | Same hash `c7d7d008…` under `PYTHONHASHSEED` 0, 1, 12345, random, in four separate processes |
| 3.12 | Version change → new identity | **PASS** | `test_a_version_change_produces_a_new_identity` |
| 3.13 | Failed replay → `non_reproducible` state + alert | **FAIL** | `grep -rn "non_reproducible"` over `app/` and `db/sql/` returns **nothing**. The state does not exist, no code detects a replay mismatch, no alert fires |
| 3.14 | Golden replay from persisted rows | **PASS** | `tests/integration/test_golden_replay.py`, 5 tests, all reading rows back out of PostgreSQL |

Tamper detection is proven: `test_a_tampered_result_is_detectable_by_recomputing_the_hash` shows one cent of drift changes the sealed hash.

---

## 4. Tax and portfolio correctness

| # | Item | Result | Evidence |
|---|---|---|---|
| 4.1 | `TaxEngineService` sole tax authority | **PASS** | `engine_evaluator()` is the only path to `compute()`; every objective value derives from an engine run |
| 4.2 | `RulesEvaluatorService` sole eligibility authority | **PASS** | `_eligibility_status()` lives in the rules service; normalization copies verbatim |
| 4.3 | Normalization never invents legal fields | **PASS** | `test_rule_without_contract_authoring_is_indeterminate_and_not_evaluable` |
| 4.4 | Contract-v1 → explicit `indeterminate` | **PASS** | Same test; `eligibility_status='indeterminate'`, `portfolio_membership='excluded_not_evaluable'`, `calculation_basis IS NULL` |
| 4.5 | Rule data references lever codes only | **PASS** | `PortfolioLeverRef` carries `lever_code` + `parameter_bindings`; `test_the_scenario_spec_stores_codes_not_engine_fields` |
| 4.6 | Totals from the final combined engine run | **PASS** | `assemble()` I-2 requires the combined run to agree with the last accepted trial within `EPSILON_ACCEPT`, else `PortfolioReconciliationError` |
| 4.7 | API never sums candidate amounts | **PASS** | `test_routes_contain_no_calculation_or_reconstruction` (AST); `test_the_portfolio_total_is_the_sealed_value_not_a_sum_of_members` |
| 4.8 | I-1 and I-2 hold on persisted rows | **PASS** | `test_stored_objective_delta_reconciles_three_ways`; `test_portfolio_reconciliation_replays_from_stored_rows` |
| 4.9 | Super/sub-additive both supported | **PASS** | `test_no_inequality_is_imposed_between_total_and_sum_of_standalone`; `_threshold_bonus_engine` / `_capped_engine` in `test_portfolio_p4_cases.py` |
| 4.10 | Shared resources cannot be over-allocated | **PASS** | `ResourceLedger.assert_conservation()`; `test_shared_resource_exhaustion_names_the_pool_that_blocked_it` |
| 4.11 | Contribution-room / cash / household conserve capacity | **PARTIAL** | Contribution-room and cash proven. **Household scope is not implemented** — `pool_scope` exists on `resource_ledger_entry` and `rule_shared_resource` but nothing ever sets it to `household` or aggregates across a household |
| 4.12 | Excluded candidates visible with reasons | **PASS** | `ioe.portfolio_exclusion` with `reason_code` + `resolution_options`; `test_insufficient_cash_is_persisted_as_a_structured_exclusion` |
| 4.13 | Eligibility-changing actions re-evaluated or flagged | **PASS** | `test_a_candidate_with_no_pinned_tree_is_marked_requires_re_evaluation`; tri-state verdict |
| 4.14 | Eight economic concepts distinct | **PASS** | Separate columns on `strategy_portfolio`; `test_commitment_concepts_are_stored_separately_not_merged` |
| 4.15 | Projections never in current-year totals | **PASS** | `test_projections_never_enter_current_year_portfolio_totals` |
| 4.16 | No projection without governed calculated impact | **PASS** | `test_only_projection_eligible_candidates_generate_projections`; `authorize()` reads rule data alone |
| 4.17 | No global-optimality claim | **PASS** | `optimality_claim='none'` persisted; `optimality_note` in every portfolio response |

### Golden examples

| Example | Result | Evidence |
|---|---|---|
| RRSP contribution | **PASS** | `test_portfolio_is_persisted_with_its_pinned_objective_and_three_values` |
| Donation | **PASS** | `test_commitment_concepts_are_stored_separately_not_merged` — donation resolves to `nonrecoverable_expenditure` and reduces the objective |
| Shared deduction pool | **PASS** | `test_shared_resource_exhaustion_names_the_pool_that_blocked_it` |
| Non-refundable credit ceiling | **FAIL** | No test exercises a credit ceiling. `_capped_engine` in `test_portfolio_p4_cases.py` simulates a cap through an injected evaluator, not through real engine credit logic |
| Tax-deferral-only strategy | **PASS** | `test_portfolio_p4_cases.py` deferral case; `total_deferral_amount` surfaces with `is_permanent: false` |
| Insufficient cash | **PASS** | `test_insufficient_cash_is_persisted_as_a_structured_exclusion` |
| Mutually exclusive strategies | **PASS** | `EXCLUDED_CONFLICT` path with `blocking_candidate_key`; `test_portfolio_p4_cases.py` |
| Eligibility-changing strategy | **PASS** | `test_a_lower_net_income_after_an_action_changes_what_rules_match` |
| Super-additive interaction | **PASS** | `_threshold_bonus_engine` case |

---

## 5. API correctness and contract safety

| # | Item | Result | Evidence |
|---|---|---|---|
| 5.1 | Routes thin; structural no-calculation test | **PASS** | `test_routes_contain_no_calculation_or_reconstruction` parses the route AST; it caught a real `sorted()` during P6 |
| 5.2 | Responses read sealed evidence only | **PASS** | `test_creating_and_reading_a_scenario_returns_sealed_values` compares every figure to the stored row |
| 5.3 | Foreign ids ownership-validated | **PASS** | `IoeReadRepository.get_scenario/get_run` raise `NotFound` for another user; 9/9 routes verified |
| 5.4 | Idempotency replay for equivalent requests | **PASS** | `test_canonically_equivalent_requests_replay_the_same_scenario` — differing label/note replay the same scenario |
| 5.5 | Reused key + different spec → 409 | **PASS** | `test_the_same_key_with_a_different_spec_is_refused` |
| 5.6 | Comparison enforces ownership/completion/sealing/compatibility | **PASS** | `ScenarioComparisonService._load_sealed` in that order; 4 API tests |
| 5.7 | Archived omitted by default | **PASS** | `test_archived_scenarios_are_filtered_from_the_default_listing` |
| 5.8 | Refresh creates new, preserves historical | **PASS** | `test_refresh_creates_a_new_scenario_and_links_supersession` |
| 5.9 | Freshness evaluated before detail response | **PASS** | `test_a_detail_read_evaluates_and_persists_freshness` |
| 5.10 | Stale labelled, never silently refreshed | **PASS** | `test_going_stale_never_alters_the_stored_result`; `test_a_stale_scenario_does_not_silently_return_to_current` |
| 5.11 | Monetary responses carry full context | **PASS** | `test_every_monetary_field_carries_its_full_context` — all 10 fields on 4 amounts |
| 5.12 | Support score not a probability | **PASS** | `SUPPORT_SCORE_DISCLAIMER` travels with every `SupportScore`; asserted in the same test |
| 5.13 | OpenAPI exposes only typed levers/assumptions | **PASS** | `test_openapi_exposes_only_typed_lever_and_assumption_inputs` |
| 5.14 | No schema accepts paths/patches/expressions | **PASS** | All IOE request schemas `additionalProperties: false`; `lever_code` pattern-bound; 20 refusal tests in `test_scenario_spec.py` |
| 5.15 | Projection endpoints return status envelopes | **PASS** | `ProjectionResponse` always has `status`; `test_no_eligible_candidates_returns_an_explicit_non_generated_status` |
| 5.16 | RFC-9457 errors with sanitized codes + correlation ids | **PASS** | `test_the_api_error_body_is_sanitized_and_correlated` — `type`/`title`/`correlation_id` present, no `Traceback`/`SELECT `/`psycopg2`/`asyncpg`/`sqlalchemy` |
| 5.17 | Error responses/codes carry no sensitive data | **PASS** | `test_persisted_error_codes_contain_no_sensitive_values` (all four failure columns, enumerated-code regex); `test_a_failing_run_records_a_code_not_a_message_containing_secrets` drives a real failure whose exception text contains a synthetic SIN |
| 5.18 | OpenAPI diff vs previous public contract | **NOT APPLICABLE** | No previously published contract exists. The IOE surface is entirely new (9 route+method pairs under `/api/v1/ioe`); no existing path or schema was modified, so no breaking change is possible. A baseline should be captured at first release |

---

## 6. Tenant isolation and database security

**A cross-tenant read was found and fixed during this gate.** See below.

| # | Item | Result | Evidence |
|---|---|---|---|
| 6.1 | Every user-derived table ENABLE + FORCE RLS | **PASS (after fix)** | `test_every_user_derived_table_in_every_schema_has_forced_rls` — catalogue-driven over 8 schemas. **Failed before `0036`**: 6 partitions of `finance.income_source` / `finance.expense_record` had neither |
| 6.2 | Policies have USING and WITH CHECK | **PASS** | `test_every_user_derived_table_has_an_ownership_policy`; `test_every_such_partition_carries_a_self_ownership_policy` |
| 6.3 | Deny-by-default on populated tables | **PASS** | `test_deny_by_default_when_app_user_id_is_unset` generates evidence first, then asserts 0 across 22 tables; `test_a_partition_denies_by_default_with_no_tenant_context` |
| 6.4 | Cross-user reads/writes fail at both layers | **PASS** | App: `NotFound` on 4 routes. DB: `test_a_second_user_sees_none_of_the_first_users_evidence`; `test_writes_through_a_partition_cannot_forge_another_tenants_row` |
| 6.5 | Pool tenant context cannot leak | **PARTIAL** | `unit_of_work` sets `app.user_id` transaction-locally (`set_config(..., true)`), so it cannot survive into the next checkout. **Not proven under concurrency** — no test interleaves two tenants on one pooled connection |
| 6.6 | Every RLS traversal column indexed | **PASS** | `test_ownership_traversal_columns_are_indexed` — 22 columns asserted against `pg_index` |
| 6.7 | Parent-qualified reads use index scans | **PASS** | P4 verification record §1: 8 read shapes, all Index Scan, 0.05–0.27 ms on 2,000 runs / 50,000 candidates / 250,000 score components |
| 6.8 | No unqualified child-table methods | **PASS** | `test_the_read_repository_never_issues_an_unqualified_child_read` — every `select()` must carry `.where()` |
| 6.9 | SECURITY DEFINER functions pin search_path | **PASS** | `test_every_security_definer_function_pins_a_search_path` — every definer function in the database, not just IOE |
| 6.10 | PUBLIC has no unintended EXECUTE | **PASS (after fix)** | `test_no_definer_function_is_executable_by_public`. Two revokes were needed: `audit.log_change()` and `ref.secure_new_partitions()` |
| 6.11 | Function owners controlled | **PASS** | `test_definer_function_owners_are_controlled` — owners ⊆ {`onyx_migrator`, `postgres`, `onyx_super`} |
| 6.12 | Role privilege boundaries documented | **PARTIAL** | Boundaries are *enforced and tested* (`onyx_app_rw`, `onyx_app_ro`, `onyx_kb_admin`, `onyx_audit_writer`, `onyx_migrator`, `onyx_freshness_worker`). No operator-facing document describes them |
| 6.13 | Restore preserves RLS/grants/ownership | **FAIL** | No restore procedure exists and no drill has been run. See §13 |
| 6.14 | Dynamic worker privilege invariant | **PASS** | `test_the_worker_role_has_no_direct_table_privilege_in_any_schema` enumerates `pg_class` across all non-system schemas (12 found) and asserts an empty allow-list; automatically covers schemas added later |

### The finding

```
SET app.user_id = '00000000-0000-0000-0000-000000000000';
SELECT count(*) FROM finance.income_source;        -- 0    policy applied
SELECT count(*) FROM finance.income_source_y2025;  -- 98   every tenant's rows
```

PostgreSQL does not inherit RLS to partitions. `16_rls_grants.sql` enabled and forced RLS on the
partitioned parents; the six partitions had none, and `onyx_app_rw` holds `SELECT` on them via the
schema-wide grant. Any query naming a partition directly — application code, a migration script,
an injection — read every user's income and expense rows.

Fixed by `0036_partition_rls`: RLS enabled and forced on all six, the parent's self-ownership
policy attached, and an event trigger so a partition created later (next year's `y2026`) is
secured at creation. Regression suite: `tests/security/test_partition_rls.py`, 6 tests.

**This defect was in the codebase before the IOE work began.** It was found only because the gate
replaced a hand-written schema list with a catalogue-driven invariant.

---

## 7. Outbox and worker safety

| # | Item | Result | Evidence |
|---|---|---|---|
| 7.1 | Business change + event commit atomically | **PASS** | `test_a_completed_analysis_emits_an_event_in_its_own_transaction`; `test_a_rolled_back_change_leaves_no_event` |
| 7.2 | Duplicate producer events harmless | **PASS** | `test_duplicate_events_are_harmless` — `ON CONFLICT DO NOTHING` on `dedupe_key` |
| 7.3 | Bounded, ordered, SKIP LOCKED, ownership, tokens | **PASS** | `test_a_claim_is_bounded_however_large_a_batch_is_requested` (100,000 → ≤200); `test_claim_ordering_is_deterministic` asserts `ORDER BY c.created_at, c.id` and `FOR UPDATE SKIP LOCKED` in `prosrc` |
| 7.4 | Two workers cannot claim the same event | **PASS** | `test_two_workers_never_claim_the_same_event`; **6 concurrent workers, 40 events: claimed=40, completed=40, events claimed by >1 worker = 0, still-claimed after run = 0** |
| 7.5 | Wrong-worker completion fails | **PASS** | `test_a_different_worker_cannot_complete_someone_elses_claim`; same for `fail` |
| 7.6 | Duplicate completion/failure idempotent | **PASS** | `test_duplicate_acknowledgement_is_a_no_op_not_an_error` |
| 7.7 | Abandoned claims recover | **PASS** | `test_an_abandoned_claim_is_recovered_and_audited` (10-min timeout); `test_a_worker_whose_claim_was_recovered_cannot_acknowledge_late` |
| 7.8 | Scope-wide events fan out per tenant | **PASS** | `ioe.fan_out_freshness_event`; relay reported `fanned_out=16` on a tax-year event |
| 7.9 | Fan-out carries no financial payload | **PASS** | Function selects `user_id` only; `test_the_claim_payload_carries_no_financial_columns` fixes the claim shape to 6 identifier/code columns |
| 7.10 | Relay processes one tenant under ordinary RLS | **PASS** | `test_the_relay_applies_events_under_ordinary_tenant_context` — user A staled, user B untouched |
| 7.11 | Privileged functions manage only outbox workflow | **PASS** | The four functions move outbox rows between four states; `test_no_privileged_function_accepts_a_table_name_or_sql_fragment` |
| 7.12 | Workers carry ids and codes only | **PASS** | `test_worker_payloads_carry_identifiers_only` inspects every task signature |
| 7.13 | Retries bounded | **PASS** | `MAX_RETRIES = 4`, backoff capped at 300 s; `test_a_failure_at_the_ceiling_terminates` |
| 7.14 | Terminal failures sanitized and audited | **PASS** | `test_a_failure_code_must_be_enumerated` rejects prose containing a synthetic SIN and amount; `test_every_claim_and_terminal_transition_is_audited` |
| 7.15 | One tenant's failure does not block others | **PASS** | Each event applies in its own transaction; a failure calls `fail_freshness_event` and the loop continues (`FreshnessRelay.run_once`) |
| 7.16 | Beat schedules documented and monitored | **PARTIAL** | Schedules are declared in `workers/celery_app.py` (relay every minute, sweep at :20 hourly). **No monitoring** — see §12 |
| 7.17 | Dead-letter runbook | **FAIL** | `claim_state='failed'` rows accumulate with no alert, no dashboard, and no documented operator procedure |

---

## 8. Freshness and lifecycle correctness

| # | Item | Result | Evidence |
|---|---|---|---|
| 8.1 | Baseline changes emit events | **PASS** | `AnalysisService.run` emits `analysis_completed` in its own transaction |
| 8.2 | Financial/profile/document changes emit events | **FAIL** | Helpers exist (`on_financial_data_changed`, `on_profile_changed`, `on_document_status_changed`) but **no caller invokes them**. `grep` shows zero call sites outside their own module |
| 8.3 | Rule publication/withdrawal/supersession emit | **PARTIAL** | Publication and supersession are wired inside `PublicationService.publish`. **Withdrawal is not** — `on_rule_withdrawn` has no caller |
| 8.4 | Reference-data changes emit | **FAIL** | `on_reference_data_changed` has no caller |
| 8.5 | Version changes detected | **PARTIAL** | `emit_version_changes()` is implemented and idempotent but is **never invoked** — not at startup, not in a task |
| 8.6 | Read-time checks catch stale before response | **PASS** | `test_a_detail_read_evaluates_and_persists_freshness` |
| 8.7 | Hourly sweep catches missed events | **PASS** | `test_a_missed_event_is_still_caught_by_the_sweep`; beat entry at minute 20 |
| 8.8 | Duplicate invalidations harmless | **PASS** | `test_replaying_a_processed_event_marks_nothing_further` |
| 8.9 | Transitions never modify sealed evidence | **PASS** | `test_going_stale_never_alters_the_stored_result` |
| 8.10 | Stale cannot silently return to current | **PASS** | `ScenarioFreshnessService.apply()` refuses that direction; `test_a_stale_scenario_does_not_silently_return_to_current` |
| 8.11 | Refresh creates new + supersession links | **PASS** | `test_refresh_creates_a_new_scenario_and_supersedes_the_old_one` |
| 8.12 | `non_reproducible` distinct from staleness | **FAIL** | The state does not exist anywhere |
| 8.13 | Archive is visibility, not deletion | **PASS** | `test_delete_archives_and_keeps_every_piece_of_evidence` — all four child counts unchanged |
| 8.14 | Account deletion / legal retention documented | **FAIL** | `audit.data_deletion_request` table exists; no policy document, no retention schedule, no implementation |

**Net effect:** of the six producer categories the gate requires, **two are wired** (analysis
completion, rule publication/supersession). Four are implemented-but-unwired. Read-time evaluation
and the hourly sweep still catch everything, so no user is shown a stale result labelled current —
but the event path, which is what makes invalidation *timely*, is largely inert.

---

## 9. Privacy and sensitive-data handling

| # | Item | Result | Evidence |
|---|---|---|---|
| 9.1 | No raw documents or SIN in IOE | **PASS** | `test_no_ioe_table_stores_a_sin_or_raw_document` scans every column of every `ioe` table |
| 9.2 | Traces don't duplicate raw inputs | **PASS** | `test_the_scenario_trace_does_not_duplicate_raw_financial_inputs` — searches whole rows as JSON for the marker value |
| 9.3 | Worker payloads carry no financial data | **PASS** | `test_worker_payloads_carry_identifiers_only` |
| 9.4 | Logs redact amounts/sensitive fields | **FAIL** | No redaction processor in `configure_logging`. Structlog emits whatever a call site passes; nothing prevents an amount being logged |
| 9.5 | Audit payloads minimized | **PARTIAL** | `freshness_outbox_audit` is minimal by construction. `audit.audit_log` uses `row_to_json` and captures **whole rows including financial columns** |
| 9.6 | Error messages sanitized | **PASS** | §5.17 |
| 9.7 | Redis holds no unbounded financial data | **PASS (by construction)** | Task signatures accept only ids and codes (§7.12). Celery result backend stores return values — currently ids and counts |
| 9.8 | Cache keys/TTLs/invalidation documented | **NOT APPLICABLE** | No application cache exists. Redis is broker + result backend only |
| 9.9 | Backups encrypted | **FAIL** | No backup configuration exists |
| 9.10 | Retention periods by record class | **FAIL** | Not documented |
| 9.11 | Deletion/anonymization/tombstoning/legal holds | **FAIL** | Not defined |
| 9.12 | Admin/support access restricted and audited | **PARTIAL** | Admin plane uses separate tokens and four-eyes for TKMS; no time-bound elevation, no break-glass procedure |
| 9.13 | Privacy threat model | **FAIL** | Does not exist |
| 9.14 | Automated scan with synthetic values | **PASS** | `tests/security/test_privilege_invariants.py` uses synthetic SIN `046454286` and amount `874321.19`; scans all four persisted failure columns and API error bodies |

---

## 10. Authentication, authorization, abuse resistance

| # | Item | Result | Evidence |
|---|---|---|---|
| 10.1 | All IOE endpoints authenticated | **PASS** | All **9/9** route+method pairs return 401 without a bearer token |
| 10.2 | User ids never trusted from request | **PASS** | `current_user_id` derives from the JWT `sub`; no route takes a user id |
| 10.3 | Ownership enforced on lookups | **PASS** | §5.3 |
| 10.4 | Rate limits on IOE operations | **FAIL** | `rate_limit_auth_per_minute` / `rate_limit_analysis_per_minute` exist in config but **no middleware reads them**. No limiter anywhere; no IOE-specific limits at all |
| 10.5 | Concurrent job limits | **FAIL** | None. Idempotency prevents duplicate *identical* runs; nothing caps distinct concurrent runs per user |
| 10.6 | Scenario sweeps bounded | **PASS** | `SWEEP_BATCH_SIZE = 200`, `limit` clamped |
| 10.7 | Request-body and list lengths bounded | **PASS** | Verified: 26 levers rejected (max 25), 26 assumptions rejected, 0 levers rejected (min 1), 5,000-char label rejected (max 200), extra field `patch` rejected |
| 10.8 | Lever/assumption values validated pre-engine | **PASS** | `ScenarioSpec.parse` → registry bounds; 20 refusal tests |
| 10.9 | Failed requests cannot create unbounded rows | **PARTIAL** | A failed run writes one `optimization_run` row plus events. With no rate limit (10.4) a caller can create these without bound |
| 10.10 | Idempotency keys length/character-bounded | **FAIL** | `idempotency_key` is unbounded `text` with no validation. Not currently reachable from the IOE API (no route accepts one), but the service and worker paths do |
| 10.11 | Correlation ids not usable for authorization | **PASS** | Used only as a log field and error-body field; no code reads it for access decisions |
| 10.12 | Trace endpoints don't expose cross-user/unpublished data | **PASS** | Trace reached only via `run_id` after `get_run` ownership check; `test_a_run_belonging_to_another_user_is_not_readable` |
| 10.13 | Abuse tests for volume and oversized inputs | **PARTIAL** | Oversized typed inputs tested (10.7). **High-volume abuse not tested** — with no rate limit there is nothing to assert |

---

## 11. Performance and scalability

Measured on this container (single process, sequential, warm after first iteration, n=12).
**This is not a load test.** No concurrency, no declared hardware class, no documented reference
workload existed to run — these numbers are a latency floor, not a capacity statement.

| Operation | p50 | p95 | p99 |
|---|---:|---:|---:|
| Optimization, cold (first per user) | 125.8 ms | — | — |
| Optimization, warm rule cache | 38.8 ms | 62.4 ms | 68.8 ms |
| Single scenario | 33.6 ms | 38.8 ms | 61.3 ms |
| Scenario comparison | 4.7 ms | 5.4 ms | 7.6 ms |
| Portfolio retrieval | 4.9 ms | 5.6 ms | 12.9 ms |
| Candidate retrieval | 2.6 ms | 3.2 ms | 5.6 ms |
| Relationship retrieval | 2.7 ms | 3.1 ms | 5.3 ms |
| Trace retrieval | 4.0 ms | 5.8 ms | 6.4 ms |
| Projection retrieval | 3.2 ms | 4.0 ms | 6.6 ms |
| Scenario detail (incl. read-time freshness) | 19.5 ms | 20.5 ms | 25.8 ms |
| Read-time freshness evaluation only | 11.9 ms | 13.3 ms | 14.1 ms |
| Outbox claim + relay pass | 2.1 ms | 2.5 ms | 4.4 ms |
| Freshness sweep batch (50) | 27.5 ms | 29.8 ms | 30.2 ms |

| # | Item | Result | Evidence |
|---|---|---|---|
| 11.1 | Sync/async routing thresholds | **FAIL** | No threshold logic. Optimization runs synchronously in-request; `run_optimization` exists as a task but no route dispatches to it |
| 11.2 | Hard time budgets enforced | **PARTIAL** | Engine-run budget enforced (`max_engine_runs`, `search_budget_exhausted`). No wall-clock budget on any request or task |
| 11.3 | Worker queues isolated | **PASS** | `celery_app.conf.task_routes` — `ioe`, `ioe_freshness`, `analysis`, 6 TKMS queues |
| 11.4 | No unqualified child scans in API paths | **PASS** | §6.8 |
| 11.5 | RLS ownership joins index-backed for keyed reads | **PASS** | §6.7 |
| 11.6 | Query counts don't grow unexpectedly per candidate | **FAIL** | **185 statements for 5 candidates; 710 for 20** — ~35.5 SQL statements per candidate, growing linearly. At 100 candidates ≈ 3,550 statements per optimization |
| 11.7 | Contract loading avoids N+1 | **FAIL** | `_load_contract_data` batches actions/documents/dependencies, but `_load_condition_tree` and `_citations` run **per rule version**, and `_impact` runs per outcome |
| 11.8 | Memory bounded | **NOT VERIFIED** | Not measured |
| 11.9 | Cache invalidation avoids stampedes | **NOT APPLICABLE** | No cache exists |

**First expected scaling bottleneck:** the per-candidate query fan-out in `RulesEvaluatorService`
(11.6/11.7). Latency is dominated by round-trips, not computation, so it degrades with published
rule count and with database latency — and a managed database with 1–2 ms RTT would turn today's
39 ms optimization into several hundred milliseconds at 100 rules.

**Capacity assumptions:** none documented. No target user count, request rate, or hardware class
has been declared, so no capacity statement can be made.

---

## 12. Observability and operations

| Signal | Result | Evidence |
|---|---|---|
| Structured logs | **PASS** | `structlog` JSON; observed: `{"method":…,"path":…,"status":401,"elapsed_ms":2.0,"correlation_id":…}` |
| Correlation ids | **PASS** | Per-request `ContextVar`, in every log line and error body |
| Workflow/run ids | **PASS** | Logged by IOE worker tasks |
| Metrics: pending/running/completed/failed/stale/non_reproducible | **FAIL** | No metrics library, no `/metrics` endpoint. `non_reproducible` does not exist |
| Worker queue depth | **FAIL** | Not exported |
| Outbox pending age | **FAIL** | Derivable from `created_at WHERE claim_state='pending'`; not exported |
| Claim timeout count | **FAIL** | Recorded in `freshness_outbox_audit` as `claim_recovered`; not exported |
| Dead-letter count | **FAIL** | `claim_state='failed'` queryable; not exported |
| Replay failure count | **FAIL** | Nothing performs replay verification in production |
| Reconciliation failure count | **FAIL** | `PortfolioReconciliationError` fails the run; not counted or alerted |
| RLS denial monitoring | **FAIL** | Not instrumented |
| Latency percentiles | **FAIL** | `elapsed_ms` is logged per request; no aggregation or histogram |
| Engine-run counts | **PARTIAL** | Persisted per portfolio (`engine_runs_used`); not exported as a metric |
| Cache hit ratio | **NOT APPLICABLE** | No cache |
| Projection generation status | **PARTIAL** | Per-response status; not aggregated |

**Alerts:** none defined for any of the nine required conditions.
**Runbooks:** none exist for any alert. `docs/` contains architecture and verification records only.

---

## 13. Deployment and recovery

| # | Item | Result | Evidence |
|---|---|---|---|
| 13.1 | Deployment order documented | **FAIL** | `deploy/docker-compose.yml` and `Dockerfile` exist; no ordering document for migrations → services → workers → beat |
| 13.2 | Rolling-deployment backward compatibility | **NOT VERIFIED** | All migrations are additive, which is favourable, but no mixed-version test has been run |
| 13.3 | Old versions cannot write invalid rows | **PARTIAL** | New CHECKs (`requires_re_evaluation`, widened taxonomies) are *widening*, so an old writer stays valid. `0034` replaced the scenario seal trigger with a wider column list — an old app version writing a sealed column would now be rejected, which is the desired direction |
| 13.4 | Workers paused/compatible during migrations | **FAIL** | No procedure. Beat runs the relay every minute; a migration would race it |
| 13.5 | Feature flags | **PARTIAL** | Only `ioe_projections_enabled`. **No flag for scenarios, refresh, or async execution** |
| 13.6 | Backups and PITR configured | **FAIL** | No configuration. Mentioned as intended in `ioe-architecture-v2.md` §"Backup / DR"; nothing implemented |
| 13.7 | Restore drill succeeds | **FAIL** | Never performed |
| 13.8 | Restore preserves hashes/RLS/grants/owners/audit | **FAIL** | Cannot be verified without 13.6/13.7. **This is the recovery path for every forward-only migration** (§2) |
| 13.9 | Rollback procedure documented | **FAIL** | None. And per §2 no per-revision rollback exists to document |
| 13.10 | Forward-only migrations identified | **PASS (this document)** | §2 — **all ten IOE migrations plus `0036`** |
| 13.11 | Partial deployment failure recovery | **FAIL** | Not documented |
| 13.12 | Celery/Redis outage degrades safely | **PASS (by design, untested)** | The outbox is the source of truth; a broker outage delays invalidation, and read-time evaluation still prevents a stale result being shown as current. No chaos test run |
| 13.13 | TKMS unavailability fails closed | **PASS** | `RuleSnapshotService.capture()` failure propagates before TX-1 commits; no run is created |
| 13.14 | Engine unavailability leaves no partial evidence | **PASS** | `test_compute_failure_marks_failed_with_sanitized_code_and_no_children`; `test_a_composite_lever_failure_leaves_no_partial_scenario_result` |
| 13.15 | Staging deployment rehearsal + rollback exercise | **FAIL** | No staging environment exists. Not performed |

---

## 14. Canadian tax product and compliance review

| # | Item | Result | Evidence |
|---|---|---|---|
| 14.1 | No guaranteed-savings/refund/CRA-acceptance language | **PASS (code strings)** | `grep` over user-facing strings finds no "guarantee", "guaranteed", "will save", "will receive". `SUPPORT_SCORE_DISCLAIMER` states it is not a probability of CRA acceptance |
| 14.2 | No implication of replacing professional advice | **PASS** | `ScenarioDetailOut.disclaimer`: "Educational information only. This is not tax advice, not a filing, and is not submitted to the CRA." |
| 14.3 | Result classes distinguished | **PASS** | `calculation_basis` ∈ {`engine_determined`, `rule_formula_determined`, `scenario_estimate`, `projection_estimate`}; `effect_type` separates deferral / refund impact / current-year reduction; `is_permanent` false for deferral |
| 14.4 | Published rules only | **PASS** | `RulesEvaluatorService.evaluate` filters `status == 'published'`; `test_draft_is_never_consumed_by_the_engine` |
| 14.5 | Citations trace to exact published versions | **PASS** | `run_rule_version` pins the exact set; `affected_rule_versions` on scenario results |
| 14.6 | Tax year and jurisdiction always visible | **PASS** | Every `MonetaryAmount` carries `tax_year`; scenario and portfolio responses carry `jurisdiction` |
| 14.7 | Deadlines from governed rule metadata | **PASS** | `rules.rule_deadline`; `_days_to_deadline` uses only rule-supplied dates |
| 14.8 | Missing documentation / indeterminate explicit | **PASS** | `evidence_status` on every amount; `eligibility_status='indeterminate'` surfaced |
| 14.9 | CRA review and unmodelled facts disclosed | **PARTIAL** | Support-score disclaimer covers CRA acceptance. **No explicit "facts we did not model" disclosure** on any response |
| 14.10 | Historical scenarios labelled under original versions | **PASS** | Sealed `version_manifest`, `objective_version`, `lever_registry_version`; superseded scenarios keep their own |
| 14.11 | No AI layer can alter figures/eligibility/citations/actions | **PASS** | The `ai` schema and service are separate; no IOE code path reads from it. Every figure derives from the engine or a sealed row |
| 14.12 | External legal/compliance sign-off | **NOT COMPLETE** | Requires review by qualified Canadian tax and privacy counsel. **Not self-certified** |

---

## 15. End-to-end user journeys

| # | Journey | Result | Evidence |
|---|---|---|---|
| 1 | New user → analysis → candidates | **PASS** | `test_generate_completes_and_persists_support_scores` |
| 2 | Calculated vs estimated outcomes | **PASS** | `calculation_basis` differs between portfolio (`engine_determined`) and scenario (`scenario_estimate`) |
| 3 | Feasible portfolio total from sealed evidence | **PASS** | `test_the_portfolio_total_is_the_sealed_value_not_a_sum_of_members` |
| 4 | Recommendation excluded for cash | **PASS** | `test_insufficient_cash_is_persisted_as_a_structured_exclusion` |
| 5 | Two recommendations sharing one limit | **PASS** | `test_shared_resource_exhaustion_names_the_pool_that_blocked_it` |
| 6 | Typed scenario run | **PASS** | `test_creating_and_reading_a_scenario_returns_sealed_values` |
| 7 | Compare compatible scenarios | **PASS** | `test_comparison_keeps_deltas_separate_by_concept` |
| 8 | Structured incompatibility error | **PASS** | `test_comparison_refuses_incompatible_baselines` → 409 with reason |
| 9 | Archive and view archived | **PASS** | `test_archived_scenarios_are_filtered_from_the_default_listing` |
| 10 | Rule published → old scenario stale | **PASS** | `test_a_rule_publication_marks_scenarios_and_runs_stale` |
| 11 | Refresh → new linked scenario | **PASS** | `test_refresh_creates_a_new_scenario_and_links_supersession` |
| 12 | Projection only when governed | **PASS** | `test_only_projection_eligible_candidates_generate_projections` |
| 13 | Explicit non-generated status | **PASS** | `test_no_eligible_candidates_returns_an_explicit_non_generated_status` |
| 14 | Cross-user access denied | **PASS** | `test_another_user_cannot_read_archive_or_refresh_a_scenario` |
| 15 | Worker retry does not duplicate evidence | **PASS** | `test_duplicate_events_are_harmless`; idempotency replay tests |
| 16 | Failed calculation leaves no partial rows | **PASS** | `test_compute_failure_marks_failed_with_sanitized_code_and_no_children` |
| 17 | Historical replay reproduces sealed hash | **PASS** | `tests/integration/test_golden_replay.py` |
| 18 | Support-score disclosure visible, non-probabilistic | **PASS** | `test_every_monetary_field_carries_its_full_context` |
| 19 | Tax-deferral output clearly non-permanent | **PASS** | `total_deferral_amount.is_permanent == False` |
| 20 | Trace links result → analysis, snapshot, versions, components, portfolio | **PARTIAL** | Every link is persisted and individually queryable (`base_analysis_id`, `run_rule_snapshot`, `version_manifest`, `score_component`, `portfolio_evaluation_step`). **No single trace endpoint assembles them** — a user cannot retrieve this as one view |

---

## 16. Risk register

| ID | Risk | Sev | Lik | Mitigation | Evidence | Residual | Owner | Blocker |
|---|---|---|---|---|---|---|---|---|
| R-01 | Incorrect tax-engine formula | Critical | Medium | Single engine authority; deterministic; reconciliation tie-out | `test_tax_engine.py`; `ReconciliationCheck` | **High** — no CPA-verified golden fixtures against published CRA figures | Tax lead | **Yes** |
| R-02 | Incorrect published eligibility metadata | Critical | Medium | TKMS four-eyes; validation reports; published-only | `test_tkms_governance.py` | Medium — governance is procedural | Tax lead | **Yes** (needs 14.12) |
| R-03 | Incomplete user data | High | High | `evidence_status`; `indeterminate` never inferred | `test_rule_without_contract_authoring_is_indeterminate…` | Low | Product | No |
| R-04 | Portfolio interaction defect | High | Low | I-1/I-2 hard failures; 9 golden cases | `test_portfolio_p4_cases.py` | Low–Medium — no credit-ceiling case (§4) | Eng | No |
| R-05 | Stale result shown as current | High | Medium | Three paths; read-time is authoritative | `test_a_detail_read_evaluates…` | **Medium** — 4 of 6 producers unwired (§8) | Eng | No |
| R-06 | Cross-tenant exposure | Critical | Low | FORCE RLS incl. partitions; catalogue-driven invariants | §6; `test_partition_rls.py` | Low — but one such defect existed until this gate | Eng | No *(fixed)* |
| R-07 | Privileged-function misuse | High | Low | 4 functions, outbox-only; pinned search_path; no PUBLIC; no table grants | `test_privilege_invariants.py` | Low | Eng | No |
| R-08 | Outbox backlog | Medium | Medium | Bounded batches; recovery; sweep fallback | §7 | **High** — no alerting or dead-letter runbook | Ops | No |
| R-09 | Replay mismatch | High | Low | Sealed hashes; immutable evidence | `test_golden_replay.py` | **High** — no production verification, no `non_reproducible` state, no alert | Eng | **Yes** |
| R-10 | Migration failure | Critical | Medium | Additive; smoke-tested on 4 database shapes | §2 | **Critical** — forward-only with no verified backup | Ops | **Yes** |
| R-11 | Privacy exposure through logs | High | Medium | Sanitized errors; minimal worker payloads | §9 | **High** — no log redaction; `audit_log` captures whole rows | Eng | **Yes** |
| R-12 | Misleading projection | High | Low | Governed metadata only; explicit status; never in totals | §4.15–4.16 | Low | Product | No |
| R-13 | Overconfidence interpretation | High | Medium | Support ≠ probability; disclaimers; `optimality_claim=none` | §14 | Medium — no user comprehension testing | Product | No |
| R-14 | Dependency outage | Medium | Medium | Outbox survives broker loss; TKMS fails closed | §13.12–13.14 | Medium — untested; unpinned deps (§1.2) | Ops | No |
| R-15 | No external tax/legal review | Critical | Certain | — | — | **Critical** | Legal | **Yes** |
| R-16 | No rate limiting | High | High | None | §10.4 | **High** — unbounded expensive operations | Eng | **Yes** |
| R-17 | Query fan-out per candidate | Medium | High | None | §11.6 — 35.5 statements/candidate | Medium | Eng | No |

---

# Release blockers

1. **No backup, PITR, or restore drill (§13.6–13.8)** — combined with all migrations being forward-only (§2), a failed migration or data-corrupting bug has **no recovery path**. This is the single most serious operational gap.
2. **No rate limiting or concurrent-job limits on IOE endpoints (§10.4–10.5)** — optimization is a multi-engine-run operation reachable by any authenticated user without any cap.
3. **No `non_reproducible` integrity state and no replay verification in production (§3.13, §8.12)** — nothing detects that sealed evidence has stopped reproducing.
4. **No log redaction (§9.4)** and **`audit.audit_log` captures whole rows including financial columns (§9.5)** — a privacy exposure in the most-retained store in the system.
5. **No alerting or runbooks for any high-severity condition (§12)** — including reconciliation failure, outbox backlog, and dead letters. Operationally blind.
6. **No dependency lock file (§1.2)** — the deployed artifact is not reproducible.
7. **Type checking fails with 67 errors and is advisory in CI (§1.5)**.
8. **External Canadian tax/legal and privacy review not performed (§14.12, R-15)**.

# Required external sign-offs

- **Canadian tax counsel / CPA** — eligibility metadata, disclosure language, deadline presentation, deferral framing. Must include golden-fixture verification of engine output against published CRA figures (R-01).
- **Privacy counsel (PIPEDA and provincial equivalents)** — retention schedule, deletion and anonymization, legal holds, audit-log minimization, cross-border storage. Nothing in §9.9–9.13 currently exists to review.
- **Independent security review** — penetration test of the API surface, the privileged outbox interface, and tenant isolation. The partition-RLS finding (§6) shows this codebase can carry an exploitable isolation defect through six phases of review.
- **Operational approval** — cannot be granted until backup/restore (§13), alerting (§12), and rate limiting (§10) exist.

# Post-release follow-ups

*(Non-blocking. No blocker is hidden here.)*

- Household-scope resource pooling (§4.11) — schema support exists, unused.
- Non-refundable credit-ceiling golden example (§4) — the one missing case of nine.
- Query fan-out in `RulesEvaluatorService` (§11.6–11.7) — batch condition trees, citations and impact formulas.
- `ruff format` adoption (§1.6) — 149 files, cosmetic.
- Assembled trace endpoint (§15.20) — links exist, no single view.
- Sync/async routing thresholds for optimization (§11.1).
- Feature flags for scenarios, refresh, async execution (§13.5).
- `CREATE INDEX CONCURRENTLY` for future migrations on populated tables (§2).
- Connection-pool tenant-leak test under concurrency (§6.5).
- Role privilege-boundary operator document (§6.12).
- "Facts we did not model" disclosure (§14.9).
- OpenAPI baseline capture at first release (§5.18).
- Test-isolation: published rules accumulate across integration tests, making assembly assertions order-dependent (worked around during this gate by asserting invariants rather than selections).

# Final recommendation

## `READY FOR CONTROLLED STAGING`

The correctness core is genuinely strong and the evidence supports it: determinism and replay hold
from persisted rows, tenant isolation is now enforced at the database with catalogue-driven
invariants that cover future schema growth, the tax and eligibility authority boundaries are intact,
the portfolio reconciles on stored rows, and 499 tests pass from a clean checkout with no skips.

It is not ready for users. It has no backup, no restore drill, no rollback path, no rate limiting,
no alerting, no runbooks, no log redaction, and no external tax, privacy, or security review. Those
are operational and legal gaps, not design gaps — but a Canadian tax product handling real financial
data cannot meet users without them.

`READY FOR PRODUCTION` is not available while any release blocker or required external sign-off
remains, and eight blockers and four sign-offs remain.

One finding deserves emphasis beyond its row in the table. The partition-RLS defect (§6) was an
exploitable cross-tenant read of every user's income and expense records. It predates the IOE work,
survived six phases of architecture review, and was found only when a hand-written schema list was
replaced with an invariant computed from the database catalogue. That is the argument for the
independent security review above: the controls that catch this class of defect are the ones that
do not depend on anyone remembering.
