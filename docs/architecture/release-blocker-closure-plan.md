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

### Closure record — step 5, sparse relationship derivation (DONE)

Remediation step 5 is closed. Steps 1–4 and 6 remain open and are the next item.

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
| **Status** | **NOT STARTED** — closable by engineering alone |

### 8. Production replay verification and `non_reproducible` state

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | Add `integrity_status` ∈ {`unverified`, `verified`, `non_reproducible`} to `ioe.optimization_run` and `ioe.scenario`, with `integrity_verified_at` and `integrity_reason_code`. A scheduled verifier samples completed records, replays the sealed hash from persisted rows (the mechanism `test_golden_replay.py` already proves), and records the outcome. A mismatch sets `non_reproducible`, emits a metric, and fires a high-severity alert. **The sealed evidence is never altered** — `non_reproducible` is a label beside it, exactly as `stale` is |
| **Evidence required** | Migration adding the columns with the state CHECK; the verifier task; the alert rule; a runbook |
| **Test / drill** | A test that tampers with a stored result via an owner connection, runs the verifier, and asserts the record becomes `non_reproducible` while the original hash and every child row remain untouched. Confirm `non_reproducible` is distinct from `stale` and that a record can be both |
| **Residual risk** | Low once implemented. Currently **High** — nothing would detect sealed evidence ceasing to reproduce |
| **Status** | **NOT STARTED** — the replay mechanism exists and is tested; only the persisted state, scheduling, and alerting are missing |

### 9. Real freshness producer wiring

| | |
|---|---|
| **Owner** | Engineering |
| **Change** | Call the existing helpers at the originating sites, inside the same transaction as the business change: **financial** — income/expense/asset/liability create, update, soft-delete in `app/services/financials/`; **profile** — `TaxProfile`/`UserProfile` updates; **document** — verification status transitions in `app/services/documents/`; **TKMS withdrawal** — `on_rule_withdrawn` in the rollback/withdrawal path; **reference data** — `on_reference_data_changed` in the engine reference-data import; **version changes** — `emit_version_changes()` at application startup |
| **Evidence required** | A call site for each of the six categories; a test per category asserting the outbox row commits with the change and rolls back with it |
| **Test / drill** | Per category: make the change, assert the event exists in the same transaction, drain the relay, assert affected records go stale and sealed evidence is untouched. Plus a **coverage test** asserting every `FreshnessEvent` member has at least one production call site — so a helper cannot go uncalled again |
| **Residual risk** | Low once wired. Currently **Medium**: read-time evaluation and the hourly sweep still prevent a stale result being shown as current, so this is a *timeliness* gap, not a correctness one |
| **Status** | **NOT STARTED** — 2 of 6 wired (analysis completion, rule publication/supersession); closable by engineering alone |

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

## Delta readiness gate

On closure, run a gate covering **only**:

- the eleven items above, each against its stated evidence and drill;
- the partition security fix (re-run `test_partition_invariant.py`);
- the full test suite, to confirm no regression;
- `alembic check`, migration smoke on all four database shapes, and the golden replay suite.

**Do not repeat the full architecture review.** Reopen an architecture item only if closing a
blocker forces a material redesign — the most likely candidate is §8, if adding `integrity_status`
to sealed records turns out to require changing how evidence is sealed rather than labelled beside
it. On current reading it does not: `non_reproducible` is a label on the record, exactly as `stale`
is, and the sealed columns stay untouched.
