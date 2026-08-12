# RESIDUAL HISTORICAL DETAIL — the fifteen, and the hole found closing them

Entry 11B6H stopped a purge that would have destroyed replay evidence. This
entry resolves what was left: fifteen engineering surfaces that survived all
four certified phases and, since 0060 detached the sealed roots from
`identity.user_account`, would have outlived the account carrying a `user_id`
that resolves to nobody.

**19 → 4 blocking. Engineering `UNCLASSIFIED_BLOCKING` = 0.** The four that
remain are billing, and they are a policy question, not an engineering one.

## The correction that changed the argument

A draft of this entry claimed eight tables were `DERIVED_DELETE` because their
values are committed into `optimization_result_hash` / `scenario_result_hash`.

**That was a category error.** A digest commits to a preimage; it does not let
you reconstruct one. Neither root stores a structured result payload —
`ioe.optimization_run` and `ioe.scenario` hold digests plus a few aggregates —
so the detailed values exist only in the tables being deleted.

The one remaining reconstruction route is re-executing the engine over retained
inputs, and it is **not durable**. Measured: with the pins untouched and the
running build's `tax_engine_version` moved on, all three replays return
`unavailable/PINNED_ENGINE_VERSION_UNAVAILABLE`. Recomputation works only while
the build that produced the evidence is still running, which is transient by
design. Pinned in `test_recomputation_is_not_a_durable_property`.

So **none** of the fifteen is `DERIVED_DELETE`. All fifteen are
`LIVE_USER_DATA_DELETE`, on the rationale that does not depend on
reconstructability: *nothing reads them once the account is gone.*

## Evidence, per table

`hash-only` = the only surviving trace is a digest. `reader` = any consumer
outside replay. `obligation` = independent retained-evidence requirement.

| table | retained canonical source | hash-only | reconstructable | post-removal reader | obligation | classification |
|---|---|---|---|---|---|---|
| `ioe.candidate_cost` | NONE | YES | NO | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.candidate_economic_effect` | NONE | YES | NO | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.confidence_component` | NONE | YES | NO | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.score_component` | NONE | YES | NO | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.recommendation_relationship` | NONE | YES | NO | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.portfolio_evaluation_step` | NONE | NO (not even hashed) | NO | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.multi_year_projection` | NONE | NO | NO | account-scoped only | NO | LIVE_USER_DATA_DELETE |
| `ioe.optimization_run_event` | NONE | NO | NO (wall-clock) | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.scenario_event` | NONE | NO | NO (wall-clock) | NO | NO | LIVE_USER_DATA_DELETE |
| `ioe.scenario_result` | NONE | YES | NO | account-scoped only | NO | LIVE_USER_DATA_DELETE |
| `ioe.scenario_input_change` | NONE | YES | NO | account-scoped only | NO | LIVE_USER_DATA_DELETE |
| `ioe.scenario_confidence_component` | NONE | YES | NO | dead code | NO | LIVE_USER_DATA_DELETE |
| `analysis.analysis_line_item` | NONE | NO | NO | NO | NO | LIVE_USER_DATA_DELETE |
| `analysis.analysis_assumption` | NONE | NO | NO | NO (no writer either) | NO | LIVE_USER_DATA_DELETE |
| `analysis.reconciliation_check` | NONE | NO | NO | NO | NO | LIVE_USER_DATA_DELETE |

**Consumer graph, measured two ways** — an AST/text scan of `app`, `workers` and
`scripts`, and a SQL statement trace of the live product read paths:

```
ACCOUNT_SCOPED_PRODUCT_READ   3   multi_year_projection, scenario_input_change,
                                  scenario_result
NO_CURRENT_READER            12
OPERATOR_SUPPORT_READ         0   admin routes cover the rule KB only
SECURITY_OR_AUDIT_READ        0   the audit trail is audit.audit_log
ACCOUNT_SCOPED_EXPORT         0   no export route exists
REPLAY_OR_INTEGRITY_READ      0   measured by 11B6H
BACKGROUND_OPERATIONAL_READ   0
```

`ioe.scenario_confidence_component` has a `read_repository` method that
**nothing calls** — not even a test. Even its account-scoped reader is dead.

None of the ORM models carries a `relationship()`, which closes lazy loading as
a hidden reader path.

### Account-scoped is not a retention reason

The three tables with a live reader are reached only through routes behind
`Depends(current_user_id)` under RLS. Once the account is gone there is no such
caller, and a same-email successor is a **different `user_id`** — asserted
directly: the successor's repository reads return empty and the detail route
raises `NotFound`, the same response as "no such scenario", so it cannot even
learn the predecessor's scenario existed. Retaining data for an interface nobody
can reach is not retention; it is residue.

## The defect found while building the phase

`onyx_app_rw` held **DELETE on all twenty-three sealed-detail tables** — the
fifteen purged here *and* the eight integrity verification reads. The only
refusal was `ioe.reject_result_mutation`, and any role can switch that off:

```sql
SET app.allow_evidence_purge = 'on';
DELETE FROM ioe.scenario_result WHERE true;   -- succeeded
```

Confirmed from a genuine LOGIN, not by reading migration text. A session GUC was
the de-facto authorisation boundary for destroying sealed replay evidence,
bounded only by RLS to the caller's own tenant.

Worse, a certified test — `test_evidence_purge_context_permits_deletion` —
asserted this as intended behaviour. It encoded the hole.

**Fixed** by revoking DELETE from `onyx_app_rw` on the twenty `ioe` sealed
tables. No application code sets that GUC (only the SECURITY DEFINER keyholes
do, and they run as the owner), so this removes a capability nothing used. The
test is rewritten to assert the opposite, and named for what it now proves.

Deliberately **not** revoked on the three `analysis` tables: they carry no
insert-only trigger, so the GUC was never their boundary. They are ordinary
tenant data whose CRUD is the PD-1 model, with RLS as the isolation boundary
that `test_one_tenant_cannot_delete_anothers_rows` proves. Revoking there would
have broken a certified invariant to fix a hole that does not exist.

## A production defect CI found, unrelated to privacy

The first full-suite run on a **fresh** database failed with a portfolio
reporting `PORTFOLIO_HASH_MISMATCH` **at baseline** — before any deletion, on an
artifact nothing had touched.

Cause: `PortfolioAssemblyState` seals exclusions as `sorted(state.exclusions)`,
keyed by candidate_key (`domain/portfolio.py:559`). `PortfolioReplayService.
_rebuild` read them back with an **unordered `SELECT`** and hashed them in raw
fetch order. The two agree only by luck — insertion order usually resembles the
sorted order — and any different plan or page layout separates them.

The consequence is the worst kind available in this system: an untampered
portfolio raises the alert whose entire meaning is *this evidence was altered*.

Fixed by sorting the rebuild's exclusions by `candidate_key`, matching the
writer. `test_the_portfolio_rebuild_orders_exclusions_the_way_the_writer_sealed_them`
asserts the ordering contract rather than trying to provoke a particular
physical row order, which no test can do reliably.

This is why the local suite passing twice was not evidence: a saturated local
database and a fresh CI database populate the fixtures differently, and only one
of them exposed the bug.

## The phase

`HISTORICAL_DETAIL_CLEANUP`, migration `0064`, SQL `58_historical_detail_cleanup.sql`.

```
SOURCE_DATA → DOCUMENTS → SCENARIO_RETENTION → HISTORICAL_DETAIL_CLEANUP
                                             → AUDIT_AUTH_DEIDENTIFICATION
```

After `SCENARIO_RETENTION` for a real reason: retention deletes scenarios that
never sealed, so running afterwards means the cleanup only ever sees scenarios
that are staying, and what it removes is exactly the derived detail of retained
artifacts.

The keyhole is account-scoped, claim-authorised, `SECURITY DEFINER` with a fixed
`search_path`, PUBLIC revoked, `EXECUTE` to `onyx_privacy_worker` alone — which
holds no DELETE on any of these tables directly, so "erase one account's detail"
never becomes "erase everyone's".

**It names all fifteen tables explicitly and never walks a parent.** Walking
`ioe.optimization_run`'s children would reach `ioe.portfolio_member` and destroy
evidence. The explicitness is the safety property.

## Two independent authorities

The disjointness invariant is only as good as the sets it compares, so they come
from different artifacts:

```
EXPECTED_PURGE_SET      from the canonical privacy registry
IMPLEMENTED_PURGE_SET   parsed from the SQL keyhole
VERIFICATION_READS      imported from the 11B6H module that measured it
```

```
IMPLEMENTED == EXPECTED                    (both directions, both empty)
IMPLEMENTED ∩ VERIFICATION_READS  =  ∅
PURGE ∪ RETAIN ∪ UNRESOLVED  =  the 15, pairwise disjoint
```

Deriving both sides from the SQL would let a table omitted from the keyhole
vanish from the implementation and from its own coverage at the same time.

## Proofs

Completion guard (structural: every purged table appears in the count function;
dynamic: removing each group moves the guard by exactly the rows removed) ·
guard-on-the-guard · GUC-is-not-authority from a genuine LOGIN · privacy worker
reaches the detail only through the keyhole · idempotent re-run · crash before /
mid / after · stale claim refused · cross-tenant rows byte-identical ·
**replay and integrity still verify after the cleanup**, with all eight
verification tables asserted still populated.

## Diagnostic terminal census

After the cleanup phase reports COMPLETE, an owner-level `DELETE` of
`identity.user_account` in a disposable database. **No terminal keyhole was
created and none exists** — this measures what a future terminal removal would
leave behind.

Measured: every purge-set table empty for that account; every
verification-required table **unchanged across both the cleanup and the
removal**; and optimization, portfolio and scenario all still `verified` with
the account gone.

The retained-evidence assertion is "whatever was there before is still there",
not "these tables are non-empty" — portfolio assembly admits no candidate once
the shared rule landscape saturates, so `ioe.portfolio_member` is legitimately
empty on some runs, and a non-emptiness assertion would fail for a reason
unrelated to the cleanup. That is the same fixture effect 11B6H hit.

Also confirmed: `identity.account_lifecycle` has no foreign key to
`identity.user_account`, so the durable record that the deletion happened
outlives the account (PD-9), and `account_lifecycle_phase` refuses DELETE
outright.

## The gap, and closing it

11B6I shipped with one gap open: after the phase reported COMPLETE, a writer
holding INSERT could put the purged detail straight back. Unreachable through
the product — the engine runs only for an authenticated user and a deleting
account is `ACCESS_DISABLED` — but "unreachable through the paths we thought of"
is not a guarantee.

It was deferred on cost. These are the hottest write paths in the system: one
optimization writes thousands of `ioe.score_component` rows, and a `FOR EACH
ROW` trigger would add a lifecycle lookup to every one.

**Closed in migration `0065` with statement-level triggers.** `REFERENCING NEW
TABLE` hands the whole inserted batch to one invocation, so the check is a single
semi-join per statement regardless of row count — O(statements), not O(rows).
The objection was to a row-level design, and the transition table removes it
rather than accepting it.

Measured, interleaved across six rounds of a full optimization writing ~1,400
detail rows:

```
cutoff ENABLED    median  542.5 ms
cutoff DISABLED   median  469.7 ms
overhead          +72.8 ms  (+15.5%)
```

Interleaved because the first attempt ran all-enabled then all-disabled and
measured the growing rule landscape instead of the trigger — it reported the
guard making things 16% *faster*, which is how a benchmark tells you it is
measuring the wrong thing.

73 ms per optimization for a hard guarantee that purged personal detail cannot
be written back. The trade is recorded rather than folded into a claim that it
is free.

The cutoff starts at the deletion **request**, not at phase completion: a write
landing before the phase would otherwise be purged silently, and one landing
after would resurrect detail. Refusing from the request covers both.

It covers exactly the fifteen purged tables and none of the eight verification
reads — those are retained evidence, never purged, so there is nothing to
resurrect and a cutoff there would refuse writes a sealed artifact needs. Both
directions are asserted, and a guard-on-the-guard proves an ordinary account can
still write its own detail, since refusing everything would satisfy the first
assertion alone.

## In-flight atomicity — the question the cutoff makes necessary

The cutoff refuses writes to the fifteen purgeable tables and deliberately does
**not** refuse the eight verification-required ones. So: if deletion is requested
while an optimization is computing, can the retained half commit before the
purgeable half is refused, leaving a half-written sealed artifact?

**Transaction shape, read from the implementation rather than inferred:**

| artifact | atomic? | evidence |
|---|---|---|
| analysis | YES | `AnalysisService.__init__(session)` opens no transaction of its own; every write joins the caller's `unit_of_work` |
| optimization | staged, but the EVIDENCE is atomic | TX-1 header → compute (no transaction) → TX-2 `_persist`: *"one atomic transaction: all children, the sealed hash, and completion"* → TX-3 failure marker |
| scenario | staged, same shape | TX-1 header → TX-2 `_persist` seals atomically → TX-3 failure marker |

**The answer is no.** Every verification-required row and every purge-detail row
is written in TX-2, so a cutoff refusal aborts both halves together. Measured on
the production path with deletion requested at the moment the engine finishes —
the worst case, since persistence then runs entirely after the cutoff is live:

```
new purge-detail rows (TX-2)        0
new verification-required rows      0
new sealed result hashes            0
events appended to a sealed run     0
runs marked completed with no hash  0
```

What *does* survive is TX-1's header and its two `pending`/`running` workflow
events, committed before the user asked to be deleted. Those are lawful
pre-cutoff writes, not a torn artifact: the run has no hash, no children, and
any replay refuses it as `SealedEvidenceIncomplete` — the state every failed run
has always left. The cleanup phase purges the events when it runs, and the
lifecycle converges to `remaining = 0`.

Three earlier versions of this test reported DID NOT RAISE while never
exercising the path: `generate` resolves idempotency in TX-1 against the
**specification hash**, so a second run over identical figures — even under a
new `analysis_id` — hands back the existing completed run and never enters TX-2.
The test now changes the account's income so the specification genuinely
differs. A test that cannot fail is worth less than no test, and this one
could not fail twice over.

## Accounting

```
70 = 66 classified
   +  4 billing   POLICY_DECISION_REQUIRED
```

```
ACCOUNT_REMOVAL_ENGINEERING_READY = YES
PRIVACY_POLICY_LAUNCH_READY       = NO
```

Launch remains blocked on billing retention policy, PD-3 (auth retention
duration) and PD-10 (deployed versioned-object-storage erasure). Those are not
engineering's to answer.

Terminal account removal is **not implemented and not enabled**. No
`terminal_remove_account`, no `complete_account_deletion`, no
`delete_account_final`, and `onyx_app_rw` still has no DELETE on
`identity.user_account`.
