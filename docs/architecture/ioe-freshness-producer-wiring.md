# Onyx Ledger — Real Freshness Producer Wiring (Entry 9)

**Status: CLOSED.** Every source domain with an authoritative mutation pathway is
wired end to end. Two domains have no such pathway and are classified
`BLOCKED_NO_AUTHORITATIVE_MUTATION_PATH` — see §3.

## 1. Freshness is not integrity

| | Question | Owner |
|---|---|---|
| **Freshness** | Is this sealed result still the *right current recommendation*? | this document |
| **Integrity** | Can this sealed result still be *reproduced* from its pinned inputs? | `ioe-production-replay-integrity.md` |

They never merge. A freshness event may move `freshness_status` and
`stale_reason_code` and **nothing else** — not a result hash, not a spec hash,
not a pinned snapshot identity, not `integrity_status`. A newer engine version
makes a result stale; it does not make it non-reproducible.

## 2. Infrastructure reused (nothing new built)

`ioe.freshness_outbox` · `FreshnessEvent` / `StaleReason` enums · `emit()` with
`ON CONFLICT DO NOTHING` on `dedupe_key` · `freshness_producers.py` helpers ·
`FreshnessRelay` · `ioe.claim_freshness_events` / `complete_` / `fail_` /
`fan_out_freshness_event` keyholes · `ScenarioFreshnessService` ·
`workers.tasks.ioe.relay_freshness_outbox`. No second event bus, outbox, worker
or stale-state implementation was added.

## 3. Producer inventory

| Source domain | Authoritative service method | Mutation | Event | Stale reason | Year scope | Production caller | E2E test |
|---|---|---|---|---|---|---|---|
| Financial | `FinancialService.add_income` | create | `financial_data_changed` | `BASELINE_INPUTS_CHANGED` | yes | **✅ wired** | ✅ |
| Financial | `FinancialService.add_expense` | create | `financial_data_changed` | `BASELINE_INPUTS_CHANGED` | yes | **✅ wired** | ✅ |
| Financial | update / delete / archive / restore / import / bulk / reclassify | — | — | — | — | **n/a — no such method exists** | — |
| Profile | `ProfileService.upsert_tax_profile` | upsert | `profile_changed` | `BASELINE_INPUTS_CHANGED` | user-wide | **✅ wired** | ✅ |
| Document | `DocumentService.confirm` | confirm extraction | `financial_data_changed` + `document_status_changed` | `BASELINE_INPUTS_CHANGED` / `BASELINE_RESULT_CHANGED` | yes | **✅ wired** | ✅ |
| Document | `DocumentService.create_upload`, `.process` | upload / extract | — (deliberately none) | — | — | **✅ correct by omission** | ✅ |
| Analysis | `AnalysisService` completion | complete | `analysis_completed` | `BASELINE_INPUTS_CHANGED` | yes | ✅ pre-existing | ⚠️ not re-verified here |
| Rules | `tkms/publication/service.py` publish / supersede | publish | `rule_published` / `rule_superseded` | `RULE_SNAPSHOT_SUPERSEDED` | year | ✅ pre-existing | ❌ **scoping unverified** |
| Rules | withdrawal | withdraw | `rule_withdrawn` | — | — | ❌ **no withdrawal service exists** | ❌ |
| Engine / reference / objective / lever / assumption registry | `VersionActivationService.activate` | activate | per component | per component | global | **✅ wired (startup)** | ✅ |
| Reference data | dedicated activation service | activate | `reference_data_changed` | `REFERENCE_DATA_CHANGED` | year | `BLOCKED_NO_AUTHORITATIVE_MUTATION_PATH` — reached via `VersionActivationService` | ✅ |

### Final classification

Every known source domain, with no blank or ambiguous state:

| Domain | Classification | Evidence |
|---|---|---|
| Financial create (income, expense) | **WIRED** | `FinancialService.add_income/.add_expense` |
| Financial update/delete/archive/import/bulk | **UNSUPPORTED_DOMAIN** | no such method exists in `FinancialService` |
| Profile — 11 tax-relevant fields | **WIRED** | `ProfileService.upsert_tax_profile` |
| Profile — display name, locale, timezone, industry, employer | **NOT_CALCULATION_RELEVANT** | `PRESENTATION_FIELDS`, disjoint from `TAX_RELEVANT_FIELDS` |
| Document upload / storage / extraction | **NOT_CALCULATION_RELEVANT** | nothing has entered the calculation yet |
| Document confirmation | **WIRED** | `DocumentService.confirm` |
| Analysis completion / supersession | **WIRED** | `AnalysisService.run` → `NEWER_ANALYSIS_AVAILABLE` |
| Rule publication / supersession | **WIRED**, jurisdiction-scoped | `tkms/publication/service.py` + `_jurisdiction_of` |
| Rule withdrawal | **BLOCKED_NO_AUTHORITATIVE_MUTATION_PATH** | no withdrawal service exists; `grep -rn "async def withdraw"` over `app/services/tkms/` returns nothing. `on_rule_withdrawn` remains callable for when one is built |
| Reference-data activation | **BLOCKED_NO_AUTHORITATIVE_MUTATION_PATH** as a *runtime* service — the version is a Python constant (`engine_data.REFERENCE_DATA_VERSION`); the deployment pathway **is** wired through `VersionActivationService` | `RUNNING_VERSIONS` |
| Registry activation (8 components) | **WIRED** | engine, reference data, objective, lever, assumption, relationship, support-score, projection |

**A producer counts as wired only when the authoritative service calls it.** A
helper, a route, a fixture or a test caller does not count.

## 4. Startup activation correction

The first cut emitted version events from application startup unconditionally.
That is unsafe: replicas start together, workers import the same configuration,
rolling deploys restart repeatedly, a process can restart with nothing changed,
and startup can precede migrations.

Activation is now a **database** decision (`ioe.active_calculation_version`,
migration `0041`):

```
begin
→ SELECT ... FOR UPDATE the component's row
→ identical?  no update, no event, report already-active
→ different?  update, bump activation_revision, emit ONE event
→ commit
```

Startup still *triggers* reconciliation but is no longer the *authority*.
`FOR UPDATE` makes ten concurrent replicas produce one activation: the nine
losers block, re-read the winner's row and take the already-active path.

Dedupe key: `activation:{version_type}:{new_version}:{revision}` — never a
timestamp. The revision makes an A→B→A cycle three distinct transitions.

Evidence: `test_activating_the_already_active_version_emits_nothing` (10
restarts → 1 event), `test_concurrent_activations_of_one_new_version_emit_exactly_one_event`
(10 concurrent → exactly 1 activation, 1 event),
`test_a_failed_event_insert_rolls_back_the_activation`.

## 5. Profile tax-relevance map

Not every profile change is a tax change. `TAX_RELEVANT_FIELDS` (11 fields:
province, residency, marital status, date of birth, student, disability,
first-time home buyer, employment type, self-employment, housing status, home
ownership) versus `PRESENTATION_FIELDS` (display name, locale, timezone,
industry, employer name). The two sets are disjoint and asserted so.

An event is emitted only when a tax-relevant field **actually moved** — compared
against the stored value, so a full PUT of unchanged values invalidates nothing.
Multiple tax-relevant fields changing in one revision produce **one** normalized
event whose token is the sorted field-name list.

The route now delegates to the service: a future import or admin write cannot
skip invalidation by not going through HTTP.

## 6. Document mutation classification

| Mutation | Event | Why |
|---|---|---|
| upload initiated / binary stored | none | storage, not calculation |
| extraction completed, unconfirmed | none | nothing has entered the calculation |
| **user confirms extracted fields** | `financial_data_changed` **+** `document_status_changed` | the confirm creates income/expense rows — real calculation inputs |
| confirm that created no rows | `document_status_changed` only | evidence moved; no input did |

## 7. Transaction atomicity

Every producer call is inside the caller's transaction. The event commits with
the mutation or not at all:
`test_a_rolled_back_mutation_leaves_no_event_and_no_row` and
`test_the_event_commits_in_the_same_transaction_as_the_mutation` (a second
connection sees nothing until commit).

## 8. Event identity

`financial:{user}:{year}:{income|expense}:{row_id}` ·
`profile:{user}:{sorted field names}` · `document:{document_id}:{status}` ·
`activation:{type}:{version}:{revision}`. No timestamps anywhere. Two distinct
source rows produce two events; a retry of the same logical mutation produces
one.

## 9. Payload privacy

The outbox carries `event_type`, `stale_reason_code`, `user_id`, `analysis_id`,
`tax_year`, `dedupe_key`. No amount, no account number, no document text, no
filename, no employer name, no SIN-like value, no exception string. The profile
key names the **field that moved, never its value** — asserted directly
(`province_code` present, `BC` absent).

## 10. Tenant and scope isolation

`test_one_users_change_never_stales_another_users_result` ·
`test_a_change_in_one_tax_year_leaves_another_year_current`. RLS categories are
kept explicitly separate — owner context, cross-tenant context, and **no**
context (deny-by-default) each have their own test, and the helpers require an
explicit viewer rather than inferring the owner.

## 10b. Jurisdiction scoping

`ioe.freshness_outbox.jurisdiction` (migration `0042`) is a **qualifier**, not a
rival scope: it narrows a tax-year event rather than replacing it, which is why
`freshness_outbox_scope_is_singular` over `(analysis_id, tax_year)` is unchanged.
NULL means "not jurisdiction-specific" — a federal rule, an engine version — and
fans out across the year exactly as every historical event does.

`RulePublicationService._jurisdiction_of` maps `FED` to NULL deliberately: a
federal rule applies everywhere, so narrowing on it would exclude every
provincial result.

Matrix: ON/2025 rule → ON/2025 stale; BC/2025 current; ON/2024 current; other
tenant current. Running, failed and unsealed targets are excluded by the
`workflow_status = 'completed'` predicate.

## 10c. Multiple stale reasons

The current model stores **one** `stale_reason_code` per target, and
`ScenarioFreshnessService.apply` only transitions a **currently-fresh** record —
`freshness_status = CURRENT` is in the WHERE clause of every invalidation. So a
record already stale for `BASELINE_INPUTS_CHANGED` keeps that reason when a
later analysis or rule event arrives: **the first cause wins and is never
overwritten.**

Causal history is not lost: every event remains in `ioe.freshness_outbox` with
its own reason, and `ioe.freshness_outbox_audit` records each claim and
transition. The reason on the record answers "why should I look at this again?",
and the first answer is as good as the last. A reason *set* would be a schema
change and is recorded here as a future option, not a gap.

## 10d. Portfolio behaviour

A portfolio is **always owned by an optimization** (`ioe.strategy_portfolio.run_id`
is NOT NULL; there is no standalone-portfolio creation path). Portfolio freshness
is therefore represented through its parent optimization's freshness, which the
relay stales via `_stale_runs_for_tenant`. There is no independent portfolio
freshness model and none is claimed.

## 10e. Performance (local Unix socket — not a managed-database claim)

| Producer | Statements | Outbox inserts | p50 | p95 |
|---|---:|---:|---:|---:|
| `FinancialService.add_income` | 4 | 1 (of 2 inserts; the other is the income row) | 4.78 ms | 5.53 ms |
| `ProfileService.upsert_tax_profile` | 4 | 1 | 4.32 ms | 5.51 ms |

**Fan-out is O(1) in statements**, measured on a polluted database:

| Targets | Fan-out statements | UPDATEs | Duration | ms/target | Peak RSS |
|---:|---:|---:|---:|---:|---:|
| 1 | 2 | 1 | 6.8 ms | 6.829 | 81.3 MiB |
| 10 | 2 | 1 | 10.1 ms | 1.010 | 81.3 MiB |
| 100 | 2 | 1 | 64.6 ms | 0.646 | 81.3 MiB |
| 1000 | 2 | 1 | 617.8 ms | 0.618 | 81.3 MiB |

One set-based UPDATE per event regardless of target count — no per-target write
in the source transaction and no N+1 at any scale. Duration grows with rows
written, which is inherent; statement count does not.

## 10f. Pollution verification

Disposable database `onyx_poll9`, separate from the clean-suite database:
**208 outbox + 416 audit rows (624 total), 78 stale scenarios, 43 stale
optimizations, 462 integrity checks, 4 distinct stale reasons, 6 relay drains,
4 integrity scheduler cycles.** Producer, freshness-event, integrity-scheduler,
Item 3B and closeout suites: **129 passed**. No test assumes an empty queue,
first queue position, a single target type, a single reason, or clean scheduler
state.

## 10g. Migration 0041 privilege audit

`ioe.active_calculation_version` holds **exclusively global configuration** —
columns are `id, version_type, active_version, activation_revision,
activated_at, created_at, updated_at`. No `user_id`, no tenant-derived column,
one row per component. That is what makes the absence of RLS correct, and it is
classified in the RLS inventory alongside `weight_config`.

| Grantee | Privilege | Why | Narrower possible? |
|---|---|---|---|
| `onyx_app_rw` | `SELECT, INSERT, UPDATE` | the activation service reads the row, inserts on first activation, and compare-and-swaps thereafter | No — all three are used |
| `onyx_app_ro` | `SELECT` | read replicas / reporting | No |

**`DELETE` was revoked.** `ALTER DEFAULT PRIVILEGES IN SCHEMA ioe`
(`16_rls_grants.sql`) grants `arwd` on every new table, so DELETE arrived
uninvited. Activation never deletes — a component that stops being governed
keeps its last activation as history — so the blanket grant is explicitly
narrowed. `onyx_freshness_worker` holds **nothing** on this table; PUBLIC holds
nothing.

## 11. Known limitations

1. `FinancialService` has only `add_income` / `add_expense`. Update, delete,
   archive, restore, import, bulk and reclassify **do not exist** in this
   repository, so there is nothing to wire — not a gap that was skipped.
2. There is **no rule-withdrawal service**, so `on_rule_withdrawn` still has no
   production caller. The helper is retained for when one is built.
3. Reference-data versions are Python constants, so there is no runtime
   `ReferenceDataService.activate_version`; the deployment pathway is wired
   through `VersionActivationService`.
4. One stale reason per target — the first cause wins and is never overwritten.
   Full causal history lives in the outbox and its audit (§10c).
5. Metrics remain the existing relay counters; no exporter and no alerting
   backend exists.
6. Portfolios have no independent freshness model; they inherit their run's
   (§10d).

## 12. Remaining risks

Domains without authoritative services (rule withdrawal, reference-data
activation, financial update/delete) cannot be wired without building those
services first · rule fan-out breadth unverified · no exporter or alerting
backend · portfolios have no independent freshness representation · in-process
counters only.
