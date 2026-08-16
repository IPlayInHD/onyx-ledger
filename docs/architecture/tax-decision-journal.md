# TAX DECISION JOURNAL (Entry: Tax Decision Journal)

The durable, append-only record of what a user DECIDED about their tax
position and what they REPORTED doing about it — the user-authority state the
Assurance Map deliberately left blank when it refused to invent a COMPLETE
status.

```
POST /api/v1/ioe/decision-journal                      open a thread
GET  /api/v1/ioe/decision-journal                      list, newest first
GET  /api/v1/ioe/decision-journal/{id}                 history + projection
POST /api/v1/ioe/decision-journal/{id}/decision        declare intent
POST /api/v1/ioe/decision-journal/{id}/action-report   report having acted
```

## 1. Four things that are never each other

| | source of truth |
|---|---|
| SIMULATED | a sealed scenario exists — says nothing about intent |
| USER DECIDED | the user declared `PROCEED / DEFER / DECLINE` |
| USER REPORTED | the user says they acted — a self-report |
| SYSTEM-OBSERVED | governed evidence readiness beside the record — supporting state, never proof the action occurred |

Nothing anywhere infers one from another. `PROCEED` is intent; execution stays
`NOT_REPORTED` until the user reports; a report never becomes "verified" —
there is no `SYSTEM_VERIFIED` value at all, because no governed source can
prove an RRSP contribution happened. Evidence readiness travels in a separate,
explicitly-labeled `evidence_context`.

## 2. Persistence — history, not a mutable note

`ioe.decision_journal` (one immutable thread) + `ioe.decision_journal_event`
(append-only history), created by `db/sql/62_decision_journal.sql` / migration
`0068`, mirroring the `scenario_event` shape.

- **Event vocabulary**: `CREATED` (opens at explicit `CONSIDERING`),
  `DECISION_RECORDED`, `ACTION_REPORTED`. A change of mind appends; the
  superseded declaration stays exactly where it was.
- **Append-only by grants, not by trigger** — a deliberate departure from
  `trg_immutable`, with a reason: journal rows are `LIVE_USER_DATA_DELETE` and
  die through the `identity.user_account` cascade fired by terminal removal,
  which does not set the evidence-purge GUC. A GUC-gated DELETE trigger would
  abort the purge. Instead `onyx_app_rw` holds `SELECT, INSERT` and nothing
  else; referential actions bypass RLS and grants, so the cascade still fires.
- **Ordering / concurrency**: a per-thread `sequence` assigned under a
  transaction-scoped **advisory lock** (`pg_advisory_xact_lock` on the thread
  id). An advisory lock rather than `FOR UPDATE` because a row lock requires
  UPDATE privilege — which the role deliberately lacks; that revocation *is*
  the append-only guarantee. `(journal_id, sequence)` UNIQUE is the backstop.
  Two concurrent appends serialize into `[1, 2, 3]`, proved over HTTP.
- **Idempotency**: explicit client `request_id` per mutation —
  `(user_id, request_id)` on threads, `(journal_id, request_id)` on events. A
  retry lands on the row it already created. No fuzzy payload matching.
- **Timestamps**: `recorded_at` is the server's clock, always.
  `user_reported_action_date` is the user's claim, validated for sanity
  (not future, not before 2000), never rewriting anything.

## 3. The pin — what informed the decision

The thread stores artifact **identities**, never content: `scenario_id`,
`scenario_result_hash`, its schema version, and the Before-You-Act
`comparison_hash` + contract version, computed at creation through the
certified pure engine over sealed rows. A v1/v2 scenario pins honest NULLs —
the journal never reconstructs the artifact it claims the user saw. Changing
current income, publishing new rules, or holding new documents afterwards
moves none of it (tested).

The sealed scenario remains the readable authority for its own content while
retention permits; both die in the same account purge, so the journal never
outlives its referent.

## 4. Evidence and deadline context

`evidence_context` on the detail read, from governed sources only:

- `sealed_readiness` — `historical_readiness` over the scenario's sealed
  requirements and sealed held-evidence snapshot. Historical; never moves.
- `current_observed_readiness` — the SAME certified primitive over the sealed
  requirements and the document types held **now**. Labeled current because it
  moves — this is the "NOV 21: evidence now ready" moment, and the test holds
  a new document and watches only this half change.
- `sealed_deadline_codes` — the pinned deadline codes from the sealed bundle.
  Current deadline state lives in the Assurance Map, deliberately apart.

Document **types** only. No document ids, buckets, object keys, or content
hashes, asserted against the fixture's real values. Older seals report
`UNAVAILABLE` with the governed reason code rather than a reconstruction.

## 5. Privacy

Both tables enter the certified deletion universe (70 → **72**, depths 1 and
2), the account-delete registry, and `app/privacy/classification.py`:
`RECOMMENDATION_DATA / SOURCE / WHILE_ACCOUNT_ACTIVE / CASCADE_DELETE`,
`rls=True`, exportable. Retention is the named policy class — no invented
durations. ENABLE + FORCE RLS with `USING` and `WITH CHECK` on both tables
(thread by `user_id`, events through the thread — the `scenario_event`
shapes). Cross-tenant reads and writes 404 identically with no existence
oracle; lifecycle cutoff inherited from `db_authed`.

Free-form notes are **deferred from v1**: they would add a `USER_FREE_TEXT`
surface this entry does not need.

## 6. Deliberately absent

- **Hashing of journal events** — no replay or tamper-evidence contract
  consumes one; RLS + grants + append-only is the integrity model. Complexity
  refused rather than copied from artifacts that need it.
- **A materialized current-state column** — the projection is a pure fold
  (`project(events)`, O(n)) and a stored duplicate could disagree with the
  history it summarizes.
- **Assurance Map integration** — the Journal now provides the
  decision/execution state a future `COMPLETE`-aware Assurance could consume;
  wiring it is recorded as follow-on work, not smuggled into this entry.
- **"Since last visit" change detection** — the Journal provides decision,
  decision-event, and action-report chronology. It does NOT provide financial
  or opportunity change history, nor an Assurance diff; those remain the
  future retention engine's.

## 7. Measured

| | |
|---|---|
| business authorities on create/append/read | engine 0 · rules 0 (counters non-vacuous; the pinned comparison hash present) |
| listing 4 threads × 11 events | **2 journal queries** (threads + one events query, no N+1) |
| projection | pure fold, O(events) |
| history integrity | UPDATE/DELETE by the app role: `permission denied`, all four statements |
