# Onyx Ledger — Release-Blocker Closure Plan

**Scope.** Finite plan to close the ten blockers from the production-readiness gate, plus the
query-fan-out beta blocker. No new architecture phase. No product features.

**Gate status accepted:** `READY FOR CONTROLLED STAGING`.
**Baseline:** commit `6d0ef6c` + partition invariant · 507 tests passing.

Owner names are **roles**, not people — this project has one engineer and no operations,
security, or legal function. Every blocker owned by a role that does not yet exist is
**staffing-blocked**, which is stated rather than glossed over. That is itself a finding: eight of
these eleven items cannot be closed by writing code.

---

## Partition security fix — acceptance condition MET

The gate accepted `0036` subject to a final invariant. That invariant now exists and passes.

| Property | Evidence |
|---|---|
| Every partition independently ENABLE + FORCE RLS | `test_every_partition_independently_enables_and_forces_rls` — asserts per partition, never inferring from the parent |
| Correct policies | `test_every_partition_has_a_correct_ownership_policy` — `cmd = ALL`, and both `USING` and `WITH CHECK` resolve to `ref.current_app_user()` |
| Minimal grants | `test_no_partition_grants_more_than_its_parent` — cross-joins every `onyx%` role against 7 privileges; a partition may never hold what its parent does not. `test_the_freshness_worker_holds_nothing_on_any_partition` |
| Deny-by-default | `test_every_partition_denies_by_default_with_no_tenant_context` — populates first so it cannot pass vacuously; `test_every_partition_is_owner_scoped_for_reads_and_writes` proves cross-tenant read and forged write both fail |
| Future partitions | `test_a_future_partition_satisfies_all_four_properties` creates `income_source_y2042` and asserts all four; `test_the_event_trigger_that_secures_new_partitions_is_installed` fails if the trigger is dropped or disabled |

`tests/security/test_partition_invariant.py` — 8 tests, all passing. **Status: CLOSED.**

---

## Query fan-out — profile and remediation plan

### The profile

Measured with a `before_cursor_execute` listener over one optimization run, 20 published rules,
75 candidates: **2,998 SQL statements, 40.0 per candidate.**

| Statement bucket | Count | Per candidate | Shape |
|---|---:|---:|---|
| `INSERT ioe.recommendation_relationship` | 1,189 | 15.9 | **O(n²) rows**, one statement each |
| `INSERT ioe.score_component` | 525 | 7.0 | 7 factors × n, one statement each |
| `SELECT` (rules/contract loading) | 304 | 4.1 | per rule version |
| `INSERT ioe.confidence_component` | 300 | 4.0 | 4 factors × n |
| `INSERT ioe.rule_snapshot_artifact` | 175 | 2.3 | per pinned version |
| `INSERT ioe.portfolio_evaluation_step` | 165 | 2.2 | per engine run |
| `INSERT ioe.run_rule_version` | 84 | 1.1 | per pinned version |
| `INSERT ioe.optimization_candidate` | 75 | 1.0 | per candidate |
| `INSERT ioe.candidate_cost` | 70 | 0.9 | per cost |
| `INSERT ioe.portfolio_exclusion` | 62 | 0.8 | per exclusion |
| everything else | 49 | 0.7 | — |

### What this actually is

The gate reported this as N+1 in `RulesEvaluatorService`. The profile shows that is **wrong**:
reads are 304 statements (10%). **90% is row-by-row `INSERT` during TX-2 persistence** — every
`session.add()` in `_persist` flushes as its own statement.

Two distinct problems, with different fixes:

**(a) Statement count is O(rows) where it should be O(tables).** Every child table is written one
row per statement. SQLAlchemy will batch these with a single `session.execute(insert(Table), [...])`
per table. This is a mechanical change to `_persist` and `PortfolioEvaluationService.persist`.

**(b) `recommendation_relationship` is O(n²) in ROWS, not just statements.** 75 candidates produced
1,189 edges. At 200 candidates that is ~8,400 rows *per run*, and batching makes it one fast
statement but does not make the row count sane. Relationship derivation needs a sparsity bound —
derive edges only between candidates sharing a resource pool or a declared dependency, which is
what the relationships actually mean.

### Explicit targets

| Metric | Now (75 candidates) | Target | Basis |
|---|---:|---:|---|
| Total statements per optimization run | 2,998 | **≤ 60, independent of candidate count** | one batched INSERT per child table (≈14) + reads (≈30) + workflow (≈10) |
| Statements per candidate | 40.0 | **≤ 1.0 at 100 candidates** | O(1) amortised |
| `recommendation_relationship` rows at 200 candidates | ~8,400 projected | **≤ 4 × candidate count** | sparsity bound: edges only where a shared resource or declared dependency exists |
| Optimization p95, 100 candidates, 2 ms DB RTT | ~6 s projected | **≤ 1.5 s** | 2,998 × 2 ms ≈ 6 s today; 60 × 2 ms ≈ 0.12 s + compute |

The 2 ms RTT figure matters: today's 38.8 ms p50 is measured over a **unix socket**. A managed
database at 1–2 ms per round trip turns 2,998 statements into roughly six seconds. The current
numbers are not reassuring, they are an artefact of co-located PostgreSQL.

### Remediation steps

1. Batch `_persist` child inserts — `optimization_candidate` (with `RETURNING id`), then
   `score_component`, `confidence_component`, `candidate_cost`, `candidate_economic_effect`,
   `recommendation_relationship`, `run_rule_version` as one `executemany` each.
2. Batch `PortfolioEvaluationService.persist` — `portfolio_member`, `portfolio_evaluation_step`,
   `portfolio_exclusion`, `resource_ledger_entry`.
3. Batch `RuleSnapshotService.capture` — `rule_snapshot_artifact`.
4. Batch the contract reads — `_load_condition_tree` and `_citations` currently run per version;
   load all versions' conditions and citations in one query each, as `_load_contract_data` already
   does for actions and documents.
5. Bound relationship derivation to candidate pairs sharing a resource pool or declared dependency.
6. Add a **regression test asserting the statement count does not grow with candidate count** —
   run at 5 and at 50 candidates, assert total statements differ by less than 20%.

**Beta gate:** step 6's test must pass, and staging must show p95 optimization ≤ 1.5 s at the
expected candidate count on the real database. Until then this remains a **beta blocker**.

### Closure record — steps 1–4 and 6, batched persistence and batched reads (DONE)

All six remediation steps are now closed and every acceptance target is met.

**Statements per optimization run, flat across the candidate population** (same fixture, freshly
provisioned database, measured with `before_cursor_execute`):

| Candidates | Total statements | Per candidate | Reads | Writes |
|---:|---:|---:|---:|---:|
| 5 | 59 | 11.80 | 36 | 21 |
| 25 | 59 | 2.36 | 36 | 21 |
| 75 | 59 | 0.79 | 36 | 21 |
| 100 | 59 | 0.59 | 36 | 21 |
| 200 | 57 | 0.28 | 36 | 19 |

The count does not move with the candidate population; the two-statement drop at 200 candidates is
two *fewer* writes, because two optional child tables had no rows. Cost now tracks the number of
tables written, not the number of rows.

**How.** Three findings drove the implementation, none of them obvious from the code:

1. **The ORM unit of work cannot batch these inserts.** A server-generated primary key needs
   `INSERT ... RETURNING`, and SQLAlchemy's `insertmanyvalues` refuses to batch a RETURNING insert
   whose row order must be preserved unless the table carries a sentinel column. Every evidence
   child therefore cost one round trip. Parent ids are now generated by the application with
   `uuid7()` — byte-for-byte the same shape as `ref.uuid_generate_v7()`, asserted against it in a
   test — so children can be built without asking the database for anything.
2. **`executemany` is not equivalent to a multi-row `VALUES`.** With `executemany`, SQLAlchemy drops
   a `None` value from the column list so the column default can fire; when rows disagree about
   which columns are `None` it recompiles and re-executes per group. Real evidence rows disagree
   constantly — an optional reason code set on some rows and not others — so the first batching
   attempt still produced 234 statements. `bulk_insert` uses `insert().values([...])`, which
   compiles one column list for the whole batch, and normalizes rows to a common key set first.
3. **Two RLS GUC round trips per transaction.** `unit_of_work` issued `set_config` for
   `app.actor_type` and `app.user_id` as separate statements; a request that opens three
   transactions paid six round trips. They are now one statement. `app.user_id` is still left UNSET
   when there is no user, because deny-by-default depends on its absence.

Reads were batched in the same pass. `RulesEvaluatorService` loaded a condition tree, a rule, its
outcomes, its citations and its impact formula **per rule version**; all five are now keyed by the
whole version set, and each distinct formula is evaluated once instead of once per outcome.
`RuleSnapshotService` did the same per condition group and per formula.

**What batching did not cost.** TX-2 remains one atomic transaction — a forced failure after the
candidate batch leaves no candidates, no relationships and no portfolio, and the run records a
sanitized `PERSISTENCE_FAILED`. The append-only trigger still rejects an UPDATE on a batched row,
and RLS still hides it from another tenant: both are server-side and per row, so the shape of the
INSERT is irrelevant to them.

**Before and after on the identical fixture** (75 candidates, freshly provisioned database):

| Metric | Before (item-1 tree) | After | Change |
|---|---:|---:|---:|
| Total statements | 2,109 | 59 | **−97.2 %** |
| Statements per candidate | 28.12 | 0.79 | −97.2 % |
| Reads | 410 | 36 | −91.2 % |
| Writes | 1,697 | 23 | −98.6 % |
| Optimization cold | 1,093.1 ms | 498.7 ms | −54.4 % |
| Optimization p50 | 952.7 ms | 394.0 ms | −58.6 % |
| Optimization p95 | 981.6 ms | 453.8 ms | −53.8 % |
| Optimization p99 | 1,021.9 ms | 676.7 ms | −33.8 % |

**The latency figures do not prove the managed-database target and are not offered as proof.** They
are measured over a unix socket, where a round trip costs almost nothing, so they mostly measure
compute. What the work actually moves is the **round-trip count**, which is what a managed database
multiplies by its RTT:

| At 2 ms RTT | Before | After |
|---|---:|---:|
| Database round trips per run | 2,109 | 59 |
| Time in round trips alone | ≈ 4.2 s | ≈ 0.12 s |
| Plus measured compute (p95) | — | ≈ 0.45 s |
| Projected p95 | ≈ 5.2 s | **≈ 0.57 s** |

The projected 0.57 s sits under the 1.5 s target with margin, but it remains a **projection**. The
beta gate still requires the measurement on the real managed database in staging.

**Evidence:** `tests/integration/test_persistence_batching.py` (15 tests).

### Closure record — step 5, sparse relationship derivation (DONE)

Derivation no longer compares every candidate with every other one. Candidates are indexed by
**relationship key** — resource code, engine input field, lever code, opportunity code — in a
single pass, and edges are generated only inside a group that a key actually populated. Within a
symmetric group the edges form a **star around the group's canonical anchor** (its lowest
candidate key) rather than a clique, so a group of *k* members costs *k−1* edges instead of
*k(k−1)/2* while every member remains an endpoint and the group is still recoverable from the
stored rows.

Declared facts are **not** starred. Explicit exclusions, dependencies and registry-declared lever
conflicts decide portfolio membership, so every declared pair is emitted exactly as declared; their
volume is bounded by the rules contract, not by *n²*. `MAX_EDGES_PER_CANDIDATE = 4` is a backstop
only, and it trims explanatory edges (`overlaps`, `shares_limit`) — never a rules-supplied or
decision-bearing one.

Every edge now persists a `derivation_source` (`rules_contract`, `shared_resource`,
`relationship_registry`, `measured_interaction`), enforced by a CHECK constraint, so an
authoritative legal fact is distinguishable from a derived structural hint at read time.

**Measured, same fixture, same clean database, 75 candidates:**

| Fixture | Metric | Before | After | Change |
|---|---|---:|---:|---:|
| 4 levers / 3 pools | relationship rows | 666 | 100 | **−85.0 %** |
| | rows per candidate | 8.88 | 1.33 | −85.0 % |
| | total SQL statements | 2,450 | 1,884 | −23.1 % |
| | statements per candidate | 32.67 | 25.12 | −23.1 % |
| 1 lever / 1 pool (worst case) | relationship rows | 2,775 | 115 | **−95.9 %** |
| | rows per candidate | 37.00 | 1.53 | −95.9 % |
| | total SQL statements | 4,558 | 1,898 | −58.4 % |
| | statements per candidate | 60.77 | 25.31 | −58.4 % |

Reads are unchanged at 260 statements in every run — confirming again that the fan-out is write
side, not the N+1 read the gate originally reported.

The `≤ 4 × candidate count` acceptance target is met with an order of magnitude of headroom.
The **≤ 60 statements per run** target is *not* yet met (1,884) and is exactly what remediation
steps 1–4 address; relationship inserts are no longer the dominant term, `score_component` (525)
and `confidence_component` (300) are.

**Evidence:** `tests/unit/ioe/test_relationship_sparsity.py` (29 tests),
`tests/integration/test_relationship_sparsity_persisted.py` (5 tests).

---

## Blocker closure table

### 1. Backup, PITR, restore drill

| | |
|---|---|
| **Owner** | Operations — *role does not exist; staffing-blocked* |
| **Change** | Enable managed PITR with encryption at rest; define RPO/RTO; document the restore procedure |
| **Evidence required** | Backup configuration; a restore of a production-shaped dump into a clean instance; post-restore verification report |
| **Test / drill** | Automated post-restore assertion suite reusing existing invariants: `test_partition_invariant.py`, `test_privilege_invariants.py`, `test_ioe_rls_inventory.py`, plus new checks that every sealed `optimization_result_hash` / `scenario_result_hash` is byte-identical, `pg_proc.prosecdef` + `proowner` unchanged, `pg_policies` count unchanged, `audit.audit_log` row count unchanged, and all 6 partitions still ENABLE+FORCE. Drill repeated quarterly |
| **Residual risk** | **High until done.** Combined with §2 there is no recovery path from a bad migration or data-corrupting defect |
| **Status** | **NOT STARTED** — blocked on operations ownership |

### 2. Forward-only migration and failed-deployment recovery

| | |
|---|---|
| **Owner** | Engineering + Operations |
| **Change** | Classify every revision explicitly. Add `FORWARD_ONLY = True` to each no-op-downgrade revision and a CI check that a revision with a `pass` downgrade carries the marker. Write the recovery strategy: restore-to-point + replay, since rollback does not exist |
| **Evidence required** | Marker present on all 36 revisions; CI check failing on an unmarked no-op; a recovery runbook naming the restore target and replay procedure |
| **Test / drill** | Staging rehearsal: apply a deliberately failing migration mid-chain, confirm the deployment halts, restore from PITR, confirm the invariant suite passes, confirm no partial schema remains. Repeat with workers running to prove the pause procedure |
| **Residual risk** | Medium after documentation; **High until the drill runs** — an untested recovery procedure is a hypothesis |
| **Status** | **NOT STARTED** — the marker and CI check are doable now; the drill needs §1 |

### 3. Rate limits, request bounds, concurrent-job limits

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | Redis-backed limiter middleware reading the existing `rate_limit_*` settings; per-user limits on `POST /scenarios`, `/refresh`, `/compare`, and optimization; a per-user concurrent-run cap enforced by a partial unique index on in-flight runs; length and character bounds on `idempotency_key` |
| **Evidence required** | Middleware wired in `app/main.py`; limits in config with production values; `429` responses carrying `Retry-After` |
| **Test / drill** | Abuse suite: 100 rapid scenario creations → `429` after the limit; N+1 concurrent optimizations → refused; oversized and malformed `idempotency_key` → `422`; a burst leaves no unbounded workflow rows. Request bounds already proven (26 levers, 26 assumptions, 5,000-char label, extra `patch` field all rejected) |
| **Residual risk** | Low once implemented. Currently **High** — optimization is multi-engine-run and uncapped |
| **Status** | **NOT STARTED** — no external dependency; closable by engineering alone |

### 4. Monitoring, alerts, operator runbooks

| | |
|---|---|
| **Owner** | Engineering (instrumentation) + Operations (alerting, runbooks) — *Operations staffing-blocked* |
| **Change** | `prometheus-client` with a `/metrics` endpoint. Gauges: workflow counts by status, outbox pending count and oldest-event age, `claim_state='failed'` count, `claim_recovered` rate, sweep lag. Counters: reconciliation failures, replay mismatches, RLS denials, HTTP status classes. Histogram: request latency (currently only logged as `elapsed_ms`) |
| **Evidence required** | `/metrics` output showing every named series; alert rules for the eight required conditions; one runbook per high-severity alert |
| **Test / drill** | Metric-presence test asserting each required series exists. Alert rehearsal: induce a stuck workflow, an outbox backlog, a forced `PortfolioReconciliationError`, and a dead letter; confirm each alert fires and its runbook resolves it |
| **Residual risk** | **High until done.** Operationally blind — a reconciliation failure or outbox backlog today produces no signal at all |
| **Status** | **NOT STARTED** — instrumentation closable by engineering; alerting and runbooks need operations |

### 5. Structured-log redaction and automated scanning

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | A structlog processor that redacts by key name (`amount`, `income`, `sin`, `balance`, `tax`, `email`, `*_amount`) and by value pattern (9-digit SIN-like, formatted SIN). Reduce `audit.log_change` from whole-row `row_to_json` to a changed-column key list plus non-financial values — **the largest remaining privacy exposure**, since audit is the most-retained store |
| **Evidence required** | Processor in `configure_logging`; audit trigger emitting minimized payloads; a documented field-classification table |
| **Test / drill** | Extend the existing synthetic-value scan (SIN `046454286`, amount `874321.19`) from persisted error fields to captured log output and `audit.audit_log` payloads. Assert neither appears in either |
| **Residual risk** | Medium after redaction — `audit_log` minimization is a schema change requiring a retention decision |
| **Status** | **NOT STARTED** — closable by engineering; the audit change needs the §10 privacy review to set retention first |

### 6. Reproducible dependency locking

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | Adopt `uv` (or pip-tools) and commit `uv.lock` with hashes. Pin the Python version. Change CI and the Dockerfile to install from the lock, not from ranges |
| **Evidence required** | Committed lock with hashes; CI installing from it; two installs a week apart resolving identically |
| **Test / drill** | CI job: fresh container, install from lock, run the suite, assert `pip freeze` matches the lock exactly |
| **Residual risk** | Low once done |
| **Status** | **NOT STARTED** — smallest blocker; no dependency on anything else |

### 7. Blocking type checking for the protected scope

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | Fix the 67 mypy errors within the protected scope: `app/services/tax_engine/**`, `app/services/ioe/**`, `app/core/security/**`, `app/schemas/**`, `app/api/**`, `workers/**`. Add per-module `strict` config for those paths. **Remove `\|\| true` from CI for the protected scope**, keeping it advisory elsewhere until the rest is cleaned up |
| **Evidence required** | `mypy` exit 0 for the protected scope; CI failing on a deliberately introduced type error |
| **Test / drill** | CI check on a branch that introduces a type error in `app/services/ioe/` — must fail the build |
| **Residual risk** | Low. Some errors are genuine (`Result[Any].rowcount` in `freshness_relay.py`, `JSONResponse` returned where `dict` is declared in `main.py`) and may surface real defects |
| **Status** | **OPEN** — re-measured at the item 3B closeout (`d97f86a`..): `mypy app/services/tax_engine app/services/ioe app/core app/schemas app/api workers` reports **59 errors in 20 files**, down from 67 only because items 1–3B fixed errors in modules they touched. Neither acceptance condition is met: exit code is not 0, and there is **no CI configuration in this repository at all** (`.github/` does not exist), so "remove `\|\| true` from CI" and the failing-build drill have nothing to act on. Closing this item now requires standing up CI as well as fixing the errors. Closable by engineering alone. |

### 8. Production replay verification and `non_reproducible` state

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | Add `integrity_status` ∈ {`unverified`, `verified`, `non_reproducible`} to `ioe.optimization_run` and `ioe.scenario`, with `integrity_verified_at` and `integrity_reason_code`. A scheduled verifier samples completed records, replays the sealed hash from persisted rows (the mechanism `test_golden_replay.py` already proves), and records the outcome. A mismatch sets `non_reproducible`, emits a metric, and fires a high-severity alert. **The sealed evidence is never altered** — `non_reproducible` is a label beside it, exactly as `stale` is |
| **Evidence required** | Migration adding the columns with the state CHECK; the verifier task; the alert rule; a runbook |
| **Test / drill** | A test that tampers with a stored result via an owner connection, runs the verifier, and asserts the record becomes `non_reproducible` while the original hash and every child row remain untouched. Confirm `non_reproducible` is distinct from `stale` and that a record can be both |
| **Residual risk** | Was **High**. Now **Low–Medium**: detection exists and is proven, but nothing invokes it on a schedule, so a mismatch is found only when someone asks |
| **Status** | **PARTIALLY CLOSED** — see the reconciliation below. Engineering half delivered; scheduling and the Ops artifacts are not. |

#### Entry 8 reconciliation (item 3 + item 3B closeout)

Measured against the stated evidence, not against "related infrastructure exists".

| Required evidence | State | Where |
|---|---|---|
| Migration adding the state columns with a CHECK | **DONE** | `0038_integrity_verification` → `db/sql/32_integrity_verification.sql`: `integrity_status`, `integrity_reason_code`, `last_integrity_checked_at`, `latest_integrity_check_id` on `optimization_run`, `scenario` and `strategy_portfolio`, each with `ck_*_integrity_status`. The state set delivered is richer than the plan asked for — `not_checked / verified / mismatch / unavailable`, with `mismatch` surfaced to users as `non_reproducible` — because "unverified" conflated "never checked" with "could not check" |
| The verifier itself | **DONE** | `app/services/ioe/replay/{resolver,services,verification}.py`. TX-1 claim → replay outside any transaction → TX-2 append outcome + move current metadata. Replays optimizations, portfolios and scenarios against **pinned** versions, refusing when a pinned executable version is not the running one |
| Sealed evidence never altered | **DONE** | `ioe.reject_result_mutation()` compares stripped row images; only the four integrity columns may move on `strategy_portfolio`. `test_integrity_verification.py` asserts the original hash and every child row survive |
| A mismatch emits a metric | **DONE** | `IntegrityMetrics` / `REASON_COUNTS` in `replay/events.py`; `ALERTING_OUTCOMES = {MISMATCH}` |
| **A *scheduled* verifier samples completed records** | **DONE** (entry 8C) | Celery task `workers.tasks.ioe.verify_sealed_integrity` on its own `ioe_integrity` queue, registered as beat entry `ioe-integrity-verification` at a configuration-driven interval (`ONYX_IOE_INTEGRITY_INTERVAL_MINUTES`, default 15 min; batch `ONYX_IOE_INTEGRITY_BATCH_SIZE`, default 10, hard-capped at 50 by the scheduler and again by the SQL). It adds no logic: it calls `IntegrityScheduler.run_cycle`, which claims through `ioe.claim_integrity_targets` and verifies through `IntegrityVerificationService`. Evidence: `tests/integration/test_integrity_scheduler_task.py` (28 tests) |
| **A high-severity alert rule** | **NOT DONE** | `log.error("alert.integrity_mismatch", …)` emits the signal; **no alerting backend exists in this repository to consume it**, and a log line is not an alert. Acceptance criteria are now written down in the runbook §13 and repeated below. Depends on §4 |
| **A runbook** | **DONE** (entry 8C) | `docs/operations/ioe-integrity-verification-runbook.md` — task name, queue, beat entry, all four config keys, worker role and privileges, manual bounded invocation, the five outcome states, first response to a mismatch, legacy-vs-regression queries, stuck-claim inspection and recovery, safe retry, pause and batch-reduction procedures, privacy restrictions, and the escalation gap. It states explicitly that no alert is configured |
| **A manual operational invocation path** | **DONE** (entry 8C) | `backend/scripts/verify_integrity.py` — bounded, goes through the same scheduler and privileged claim interface, exits `2` on mismatch. No HTTP endpoint was added |
| Drill: tamper via an owner connection, verify, assert `non_reproducible` | **DONE** | `test_scenario_integrity_closeout.py::test_a_frozen_scenario_whose_result_was_altered_is_a_genuine_mismatch` — tampers with the sealed `scenario_result_hash` over a superuser connection (the application cannot: the column is sealed) and asserts `mismatch` / `non_reproducible` |
| Drill: `non_reproducible` distinct from `stale`, and a record can be both | **DONE** | `test_scenario_integrity_closeout.py::test_freshness_may_label_stale_without_moving_any_pin_or_result` — the world changes, freshness may relabel, no pin or stored number moves, and the replay still verifies |

**Closeout finding.** The item 3B closeout also found that `mismatch` was being applied to
results that predate the frozen-input correction, which asserts a deterministic replay
regression that nothing demonstrates. Corrected: `LEGACY_EXECUTION_POLICY_UNVERIFIABLE`
(migration `0040_legacy_integrity_reason`) makes those `unavailable` with a distinct reason,
distinct user-visible state (`legacy_unverifiable`), distinct wording, and a distinct metric
so readiness reporting does not count age as failure. Evidence:
`test_scenario_integrity_closeout.py` (30 tests) and
`docs/architecture/ioe-frozen-scenario-baseline-integrity.md` §6b.

**Entry 8C (scheduled verification + runbook) closed** at the commit adding
`workers.tasks.ioe.verify_sealed_integrity`. Verification now runs on a schedule instead of
only when the API is called, and an operator has a documented bounded manual path.

**To close entry 8 completely — one requirement remains, the high-severity alert.** It is
blocked on §4 (monitoring), which is staffing-blocked. Acceptance criteria, so the next person
does not have to re-derive them:

1. A log or metric pipeline ingesting `alert.integrity_mismatch`
   (`app/services/ioe/replay/events.py`).
2. A high-severity page on `integrity_verification_mismatch > 0` that **excludes**
   `LEGACY_EXECUTION_POLICY_UNVERIFIABLE` and `integrity_batch_unavailable_dependency` —
   age and transient dependency gaps must not page.
3. A named on-call rotation to receive it.
4. A test or configuration check proving the rule fires on a synthetic mismatch.

Until all four exist, detection depends on a human running the queries in runbook §7.

### 9. Real freshness producer wiring

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | Call the existing helpers at the originating sites, inside the same transaction as the business change: **financial** — income/expense/asset/liability create, update, soft-delete in `app/services/financials/`; **profile** — `TaxProfile`/`UserProfile` updates; **document** — verification status transitions in `app/services/documents/`; **TKMS withdrawal** — `on_rule_withdrawn` in the rollback/withdrawal path; **reference data** — `on_reference_data_changed` in the engine reference-data import; **version changes** — `emit_version_changes()` at application startup |
| **Evidence required** | A call site for each of the six categories; a test per category asserting the outbox row commits with the change and rolls back with it |
| **Test / drill** | Per category: make the change, assert the event exists in the same transaction, drain the relay, assert affected records go stale and sealed evidence is untouched. Plus a **coverage test** asserting every `FreshnessEvent` member has at least one production call site — so a helper cannot go uncalled again |
| **Residual risk** | Low once wired. Currently **Medium**: read-time evaluation and the hourly sweep still prevent a stale result being shown as current, so this is a *timeliness* gap, not a correctness one |
| **Status** | **OPEN** — re-measured at the item 3B closeout. **Zero of the six required categories are wired.** A repository-wide search for producer calls outside `freshness_events.py` / `freshness_producers.py` finds exactly three call sites: `analysis/service.py:104` (`ANALYSIS_COMPLETED`) and `tkms/publication/service.py:77,94` (`RULE_SUPERSEDED`, `RULE_PUBLISHED`) — none of which is one of the six. Financial, profile, document, TKMS-withdrawal, reference-data and version-change producers have no call site, and the `FreshnessEvent` coverage test the plan asks for does not exist. The earlier "2 of 6" wording counted events that are not on the list. Closable by engineering alone. |

### 10. External sign-offs

| | |
|---|---|
| **Owner** | Founder / Legal — *engagements not commenced* |
| **Change** | Engage four independent reviews |
| **Evidence required** | **Canadian tax counsel / CPA** — eligibility metadata, disclosure language, deadline presentation, deferral framing, plus golden fixtures verifying engine output against published CRA figures (R-01, currently unmitigated). **Privacy counsel (PIPEDA + provincial)** — retention schedule by record class, deletion/anonymization/tombstoning, legal holds, audit-log minimization, cross-border storage; none of §9.9–9.13 exists yet, so there is presently nothing to review. **Independent security review** — penetration test of the API, the privileged outbox interface, and tenant isolation. **Operational approval** — contingent on §1, §3, §4 |
| **Test / drill** | Written sign-off per review; findings tracked to closure |
| **Residual risk** | **Critical until complete.** The partition-RLS defect shows this codebase can carry an exploitable isolation flaw through six phases of internal review — internal review is not a substitute |
| **Status** | **NOT STARTED** |

---

## Dependency order

```
6 (locking) ─┐
7 (types)  ─┼─► independent, start immediately
3 (limits) ─┤
9 (wiring) ─┘

8 (replay state) ──► needs 4 for alerting
5 (redaction) ────► audit minimization needs 10-privacy for retention
4 (monitoring) ───► instrumentation now; alerts/runbooks need Ops
1 (backup) ───────► needs Ops
2 (recovery) ─────► needs 1
10 (sign-offs) ───► independent; long lead time — start first
query fan-out ────► independent; beta gate
```

**Start today, no dependencies:** 6, 7, 3, 9, query fan-out, and the §8 schema work.
**Start today, longest lead:** 10 — engagements take weeks and gate everything.
**Blocked on staffing:** 1, 2, and the alerting half of 4.

---

## Controlled staging conditions

Staging may proceed under the gate's recommendation, with these constraints held for its duration:

1. **Synthetic or tightly controlled non-production data only.** No real SINs, no real financial
   records, no production CRA correspondence. Seeded fixtures or consented internal data.
2. **Restricted users** — named internal accounts only. No public signup, no external testers.
3. **Explicit monitoring** — until §4 lands, a named engineer reviews structured logs daily for
   `PORTFOLIO_ASSEMBLY_FAILED`, `RETRIES_EXHAUSTED`, 5xx, and outbox rows in `claim_state='failed'`.
4. **No production-readiness claim** — not in the product, not in marketing, not to testers. The
   `SUPPORT_SCORE_DISCLAIMER` and educational disclaimer are necessary but not sufficient; staging
   users must be told directly that this is pre-release software under evaluation.
5. **No data migration path assumed** — staging data is disposable. With no backup (§1) and
   forward-only migrations (§2), staging data must be treated as unrecoverable.

---

## Blocker status — as measured, not as remembered

Every status below was re-derived from the repository during the item 3B closeout. A row may
only say CLOSED if its own **Evidence required** line is satisfied; related infrastructure
existing is not closure. The date and commit are the last time the row was *measured*, not the
last time it was edited.

| # | Item | Status | Measured at | How it was measured |
|---|---|---|---|---|
| — | Partition security fix | **CLOSED** | item 1 | `tests/security/test_partition_invariant.py`, 8 tests |
| — | Query fan-out | **CLOSED** (steps 1–6) | items 1–2 | statement counts before/after on a clean database |
| 1 | Backup, PITR, restore drill | **OPEN** | — | staffing-blocked (no Ops function) |
| 2 | Forward-only migration recovery | **OPEN** | — | staffing-blocked; depends on 1 |
| 3 | Rate limits, request bounds | **OPEN** | — | not started; closure-phase item 5 |
| 4 | Monitoring, alerts, runbooks | **OPEN** | — | staffing-blocked for the alerting half |
| 5 | Log redaction and scanning | **OPEN** | — | not started |
| 6 | Reproducible dependency locking | **OPEN** | — | not started; closure-phase item 6 |
| 7 | Blocking type checking | **OPEN** | 3B closeout | `mypy` over the protected scope → **59 errors in 20 files**; no `.github/` in the repository, so there is no CI to gate |
| 8 | Replay verification + `non_reproducible` | **PARTIALLY CLOSED** | entry 8C | evidence table above: schema, verifier, immutability, metrics, both drills, **scheduled task + beat registration + runbook + manual invocation** all done; **the high-severity alert alone remains**, blocked on §4 |
| 9 | Real freshness producer wiring | **OPEN** | 3B closeout | producer call-site search → **0 of 6 required categories**; the three existing call sites are not among them |
| 10 | External sign-offs | **OPEN** | — | staffing-blocked; long lead time |

**Rule for the final report.** The delta-readiness report must recompute this table from the
repository — `mypy` over the protected scope, the producer call-site search, the `beat_schedule`
contents, and the per-item test files — and must not copy a status from an earlier revision of
this document. A status with no *Measured at* entry is unverified and counts as OPEN.

---

## Delta readiness gate

On closure, run a gate covering **only**:

- the eleven items above, each against its stated evidence and drill, **re-measured** per the
  rule above;
- the partition security fix (re-run `test_partition_invariant.py`);
- the full test suite, to confirm no regression;
- `alembic check`, migration smoke on all four database shapes, and the golden replay suite.

**Do not repeat the full architecture review.** Reopen an architecture item only if closing a
blocker forces a material redesign — the most likely candidate was §8, if adding
`integrity_status` to sealed records turned out to require changing how evidence is sealed
rather than labelled beside it. **It did not.** `non_reproducible` is a label on the record,
exactly as `stale` is; the sealed columns stay untouched, and `ioe.reject_result_mutation()`
enforces that by comparing stripped row images rather than by trusting the caller. No
architecture item was reopened by items 3, 3A or 3B.
