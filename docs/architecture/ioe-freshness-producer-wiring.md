# Onyx Ledger — Real Freshness Producer Wiring (Entry 9)

**Status: PARTIALLY CLOSED.** Financial, profile, document/evidence and
version-activation domains are wired end to end. Analysis supersession, rule
publication scoping and registry activation are **not** — see §12.

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
| Reference data | dedicated activation service | activate | `reference_data_changed` | `REFERENCE_DATA_CHANGED` | year | ❌ **versions are code constants; activation is the only pathway** | via activation |

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

## 11. Known limitations

1. `FinancialService` has only `add_income` / `add_expense`. Update, delete,
   archive, restore, import, bulk and reclassify **do not exist** in this
   repository, so there is nothing to wire — not a gap that was skipped.
2. Rule-publication events exist but their **fan-out scoping is unverified**:
   whether a jurisdiction-specific publication stales only that jurisdiction has
   not been demonstrated.
3. There is **no rule-withdrawal service**, so `on_rule_withdrawn` still has no
   production caller.
4. Reference-data versions are Python constants; activation is the only
   pathway, so there is no separate `ReferenceDataService.activate_version`.
5. Analysis supersession (`NEWER_ANALYSIS_AVAILABLE`) is not implemented; the
   existing `analysis_completed` event does not distinguish "newer analysis
   available" from "source data changed".
6. Metrics for the freshness pipeline remain the existing relay counters; no new
   metric surface was added and there is no exporter.

## 12. Remaining risks

Domains without authoritative services (rule withdrawal, reference-data
activation, financial update/delete) cannot be wired without building those
services first · rule fan-out breadth unverified · no exporter or alerting
backend · portfolios have no independent freshness representation · in-process
counters only.
