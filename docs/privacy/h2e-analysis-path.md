# Entry 11B5H2E — the analysis start path, traced from code (§3, §4, §25)

Read from the repository, not inferred from names. File:line references are to
the state at `b001ae7`.

## Sequence: `POST /api/v1/analysis`

| # | Step | Where |
|---|------|-------|
| 1 | HTTP entry `run_analysis` | `app/api/v1/analysis/routes.py:20` |
| 2 | `current_user_id` — JWT decode, no DB | `app/api/deps.py` |
| 3 | **`db_authed` opens the request transaction** | `app/api/deps.py:30` |
| 3a | `unit_of_work(user_id, actor_type="user")` → `session.begin()` | `app/database/session.py:32` |
| 3b | `set_config('app.actor_type', …), set_config('app.user_id', …)` — both transaction-local | `session.py:47` |
| 3c | **`assert_may_act`: `pg_advisory_xact_lock_shared(identity.lifecycle_lock_key(uid))`** | `lifecycle.py:230` |
| 3d | separate statement: `SELECT state FROM identity.account_lifecycle WHERE user_id = :uid` | `lifecycle.py:235` |
| 3e | any row in `_BLOCKING_STATES` → `AccountDeletionInProgress` | `lifecycle.py:239` |
| 4 | `admission_guard(ANALYSIS_RUN, …)` — **its own** `unit_of_work(actor_type="system")` | `admission/guard.py:87` |
| 5 | `AnalysisService.run(user_id, tax_year)` | `analysis/service.py:35` |
| 5a | `build_input_from_live_sources` → `_build_input_live` | `tax_engine/service.py:59,71` |
| 5b | SELECT `TaxProfile` | `service.py:72` |
| 5c | SELECT `IncomeType` | `service.py:77` |
| 5d | SELECT `IncomeSource` WHERE `deleted_at IS NULL` | `service.py:78` |
| 5e | SELECT `ExpenseCategory` | `service.py:88` |
| 5f | SELECT `ExpenseRecord` WHERE `deleted_at IS NULL` | `service.py:92` |
| 6 | `engine.run(inp)` — pure, no DB | `analysis/service.py:37` |
| 7 | INSERT `AnalysisRun` + flush | `analysis/service.py:40,46` |
| 8 | **snapshot freeze**: `canonical_snapshot(inp)` + `snapshot_hash` → INSERT `AnalysisInputSnapshot` | `analysis/service.py:51,53` |
| 9 | INSERT line items, reconciliation check, recommendations | `analysis/service.py:58,66,75` |
| 10 | `emit(ANALYSIS_COMPLETED)` → `ioe.freshness_outbox` INSERT, same transaction | `analysis/service.py:108` |
| 11 | **COMMIT** at `db_authed`'s `unit_of_work` exit → advisory lock released | `deps.py:50` |

## The cutoff (§4)

The authoritative cutoff is **`AccountLifecycleService.assert_may_act`**, reached
through the `db_authed` dependency — not a service-level check inside
`AnalysisService`, which has none.

`_BLOCKING_STATES = frozenset(LifecycleState)` (`lifecycle.py:74`) — **every**
state blocks. So the cutoff is the *existence* of an `identity.account_lifecycle`
row, not any particular state.

`assert_may_act` is deliberately two statements: the lock is granted in its own
statement so the `SELECT state` that follows takes a fresh snapshot including
whatever committed while it waited. Under READ COMMITTED a single combined
statement would fix its snapshot before waiting and report the account active
anyway.

## Locks (§25)

| Mechanism | Holder | Mode | Scope |
|---|---|---|---|
| `pg_advisory_xact_lock_shared(lifecycle_lock_key(uid))` | every governed request via `db_authed` | **shared** | request transaction |
| `pg_advisory_xact_lock(lifecycle_lock_key(uid))` | `request_deletion` | **exclusive** | its transaction |
| `identity.purge_source_data` | privacy worker | **no advisory lock** | authorised by claim token + lifecycle state |
| admission dedupe/concurrency | `admission_guard` | separate transaction | not on the lifecycle key |
| snapshot freeze | — | no lock; derived from the single in-memory `inp` | — |

## The coherence argument, stated exactly

`_build_input_live` issues **five separate statements** under **READ COMMITTED**
(`SHOW default_transaction_isolation` = `read committed`, and no
`execution_options(isolation_level=…)` anywhere in `session.py`). Each statement
therefore takes its **own** snapshot. There is *no* isolation-level protection
against a concurrent commit landing between the income read (5d) and the expense
read (5f).

Snapshot coherence comes from **lock ordering alone**:

* a purge cannot run until an `account_lifecycle` row exists;
* that row is written by `request_deletion`, which needs the **exclusive**
  lifecycle lock;
* an in-flight analysis holds the **shared** lifecycle lock for its entire
  transaction, because `pg_advisory_xact_lock_shared` is transaction-scoped and
  `db_authed` yields that same session to the route;
* therefore `request_deletion` blocks until the analysis commits, and no purge
  can interleave with steps 5b–5f.

This is worth stating plainly because it is fragile in a specific way: moving
the cutoff off `db_authed`, committing mid-request, or running the analysis on a
second session would each silently remove the protection while leaving every
line of `AnalysisService` unchanged.

Individual deletion (`delete_income_source`, `delete_expense`) also runs under
`db_authed`, so it holds the **shared** lock too — and shared locks do not block
each other. It is nonetheless coherent, for a different and weaker reason: each
of those operations writes exactly **one** of the tables the snapshot reads, so
a commit landing between 5d and 5f changes only rows the analysis has either
already read or not yet read, never both. The hybrid risk belongs to
`purge_source_data`, which deletes income, expenses and profile **in one
transaction** — and that is the operation the lock ordering excludes.
