# Account deletion orchestration (Entry 11B2)

**This deletes no user data.** Financial records, documents, frozen snapshots,
scenarios, optimizations, AI conversations, freshness and integrity history are
all exactly where they were. What Entry 11B1 builds is the machinery a deletion
runs *through*, so that when a later phase does the deleting it is doing so
under a request that cannot be lost, bypassed or forged.

An account that has requested deletion here is **frozen, not erased**: it cannot
log in, cannot hold a session, cannot start work, cannot write, and carries a
durable record saying a purge is owed.

Nothing in this document is a statement about PIPEDA, the CPPA, CRA
requirements, or any other legal obligation. It describes what the software
does.

---

## 1. Where this sits in the plan

The Entry 11A plan (`data-lifecycle-specification.md` §26) numbered the phases
itself, and its numbering is **not** the numbering used here:

| Phase | Scope | Status |
|---|---|---|
| 11B0 | PD-4 — audit log accumulating whole user rows | **closed** |
| 11B1 | PD-1 — RLS on the 16 tenant-owned child tables | **closed** |
| **11B2** (this document) | Account lifecycle and deletion orchestration | **closed** |

This work was merged under the label "11B1" and is now numbered **11B2**: the
11A plan had already assigned 11B1 to the RLS remediation. The label moved, the
work did not — the commits keep their hashes and dates, and this was genuinely
built before 11B0.

The plan sequenced PD-4 and PD-1 first, on the reasoning that deleting data
across an incomplete tenant boundary is the wrong order to do two risky things
in. That reasoning still stands and is untouched by this entry — but it applies
to the phases that *delete*, and this one does not. The two tables added here
both carry RLS from the migration that creates them.

**Consequence to carry forward:** PD-4 was closed in Entry 11B0 and PD-1 in
Entry 11B1. Both prerequisites the 11A plan named are now met.

---

## 2. The states

There is no `ACTIVE` state. **An account with no lifecycle row is active.** The
table is empty for every account that never asks to be deleted, needs no
backfill, adds nothing to the write path of an ordinary account, and cannot
drift out of sync with the accounts it does not describe.

| State | Meaning |
|---|---|
| `DELETION_REQUESTED` | The request is durable. Access is already off. |
| `ACCESS_DISABLED` | A worker has confirmed and recorded the access cutoff. |
| `PURGE_PENDING` | Ready for a purge phase. **Where Entry 11B1 stops.** |
| `PURGING` | A purge phase is running. No phase implements this yet. |
| `COMPLETE` | Data is gone. **Unreachable in 11B1, by design.** |
| `FAILED_RETRYABLE` | A phase failed with a closed code; it will be retried. |

`TERMINAL_FOR_11B1 = PURGE_PENDING`. Reaching it means access is off and a purge
is owed. It does not mean anything has been deleted, and the API does not say it
has: the status endpoint reports `deletion_requested` throughout.

## 3. The transition table

```
DELETION_REQUESTED → ACCESS_DISABLED | FAILED_RETRYABLE
ACCESS_DISABLED    → PURGE_PENDING   | FAILED_RETRYABLE
PURGE_PENDING      → PURGING         | FAILED_RETRYABLE
PURGING            → COMPLETE        | FAILED_RETRYABLE
FAILED_RETRYABLE   → DELETION_REQUESTED | ACCESS_DISABLED | PURGE_PENDING | PURGING
```

Recovery returns to the phase that failed, never forward. A same-state update is
claim bookkeeping, not a transition, and is allowed.

`COMPLETE` is reachable **only** from `PURGING`, and the row-level CHECK
requires `completed_at` to be set exactly when the state is `COMPLETE`. Together
those make "an account was marked deleted before a purge ran" a constraint
violation rather than a support ticket. The service refuses `COMPLETE` outright,
so both the database and the code have to be defeated to tell that lie.

Enforced in **two** places on purpose. The service is the API everything should
use; the trigger is what makes "everything" true, because a migration, a psql
session or a repository method written next year cannot walk an account from
`DELETION_REQUESTED` to `COMPLETE`. All 36 ordered pairs are tested, against a
transition table restated independently of the trigger — a test that imported
the rule from the thing it tests would agree with a wrong rule.

---

## 4. The cutoff

`requested_at` is the authoritative instant, and it is the reason the rest of
this entry is shaped the way it is: later phases compare user data against it to
separate data that existed before the request from data written after it.

Three properties, each of which was wrong at some point during this entry:

**It comes from the database clock.** Not from an API process, or the comparison
would depend on which machine happened to serve the request.

**It is `clock_timestamp()`, not `now()`.** `now()` is the transaction *start*
time, so a request that waited would be stamped with the moment it began
waiting — before writes it actually waited for.

**It is frozen.** The trigger rejects any update that moves it. Moving the
cutoff would let a later phase mistake post-request writes for pre-request ones.

### 4.1 Ordering the cutoff against writes in flight

Refusing new work at the start of a request is not enough. The check is correct
when it runs and stale by the time the write lands:

```
T0  write request starts, reads lifecycle state → nothing, proceeds
T1  deletion request commits
T2  write commits
```

The row is now dated after the cutoff, where a purge bounded on `requested_at`
never looks — so an account reported as deleted would still have data. All four
user-data surfaces (income, expenses, analysis runs, documents) reproduced this.

The two transactions therefore order against each other with a
transaction-scoped advisory lock keyed on the account
(`identity.lifecycle_lock_key`):

* every user-bound request takes it **shared** — they do not conflict with each
  other;
* a deletion request takes it **exclusive** — it conflicts with all of them.

Whichever wins, the result is truthful. If writes hold it, the deletion waits
and is stamped after they land. If the deletion holds it, the write waits, then
reads the row it was waiting for and is refused.

Advisory rather than a row lock on `identity.user_account`, because
`last_login_at` is written there and a shared lock held for the length of every
authenticated request would put logins behind unrelated work.

**The check is two statements, deliberately.** Combined into one it still blocked
for the deletion and then admitted the write anyway: under `READ COMMITTED` a
statement's snapshot is taken when the statement begins, so a `SELECT` that waits
for a lock partway through reads the world as it was before it waited.

---

## 5. Idempotency

The primary key is the account. Twenty repeated requests and ten concurrent ones
each produce exactly one lifecycle row and exactly one `DELETION_REQUESTED`
event; the losers of the insert race read back the row the winner created. There
is no check-then-insert and therefore no window between looking and creating.

A repeat is a no-op, not a re-run: no second revocation storm, no second event.

**There is no cancellation.** Whether a requested deletion may be withdrawn, and
within what window, is a policy question this repository is not entitled to
answer — recorded as `POLICY_DECISION_REQUIRED`. The absence is deliberate;
inventing a grace period would be inventing policy.

---

## 6. Disabling access

Requesting deletion revokes every refresh session the account holds, in the same
transaction as the lifecycle row. A deletion that disabled access without
recording why, or recorded a request without disabling access, would be a worse
state than either outcome.

Access tokens are self-contained and cannot be recalled; they expire on their
own. Every path that would honour one is closed instead:

| Path | Where the cutoff is applied |
|---|---|
| Login | `AuthService.authenticate`, **after** credential verification |
| Refresh | `AuthService.refresh`, after rotation |
| Every authenticated route | `db_authed` |
| Routes that open their own session | `assert_account_active` |
| Admission (including worker-initiated) | `admission_guard` |
| Queued tasks | `refuse_if_deleting` preflight |

Login checks **after** verifying the password on purpose. Checking first would
answer "is this address being deleted?" to anyone who typed it, which is a worse
disclosure than the one it saves. A caller who reaches the check has already
proved they own the account.

### 6.1 The trap in reading the lifecycle row

Login, admission and the worker preflight all run **without** `app.user_id`.
RLS therefore hides the lifecycle row from them — correctly — so a direct
`SELECT ... FROM identity.account_lifecycle` on those paths sees nothing and
admits every deleting account. The query looks right and does the opposite of
what it claims.

Those three paths read through `identity.account_deletion_state(uuid)`, a
`STABLE SECURITY DEFINER` function that takes an account id and returns a state
or `NULL` — no email, no timestamp, no claim, no failure code. A structural test
asserts that none of them reads the table directly.

### 6.2 Coverage is enforced, not reviewed

`tests/unit/test_lifecycle_boundaries.py` walks the real FastAPI dependency tree
and requires every tenant-facing route to meet the cutoff or appear on an exempt
list with a reason. This is not decoration: six scenario routes opened their own
unit of work, never passed through `db_authed`, and inherited nothing. Three of
them wrote user data for a deleting account — archiving a scenario, unarchiving
one, and *reading* one, which persists the freshness transition it just
evaluated. The other three were refused only by admission, and so came back as
`429 Too Many Requests`: a deleting account being told to retry later.

The two exempt routes are `POST` and `GET /account/deletion`. Requesting
deletion must stay idempotent, and a user must be able to read the status of the
thing they asked for; both are keyed on the caller's own token and neither
creates user data.

The same file requires every Celery task taking a `user_id` to consult the
preflight. The rule keys on that parameter rather than on
`unit_of_work(user_id=...)`, because `run_optimization` opens a *system* unit of
work and still writes an optimization run for the account it was handed.

---

## 7. Queued work

A task published at T1, a deletion requested at T2, a worker picking the task up
at T3. Nothing about the queue prevents T1 < T2 < T3 — the message was published
before anyone asked to be deleted, and it sits in a broker that knows nothing
about privacy.

Celery `revoke` does not close this. A reserved task is already in a worker's
hands, `acks_late` redelivers it after a restart, and revocation is best-effort
broadcast state that a worker which was offline never sees. The only reliable
refusal is in the same database the request was recorded in, immediately before
the work commits.

`refuse_if_deleting` **returns** rather than raises. A refused task has not
failed; raising would put it through the retry ladder, log an error, and — until
Entry 11A closed that path — write an exception into the result backend.

Zero tasks reach the broker while an account is refused. Asserted twice: `app/`
contains no publication site at all (Entry 10's structural scan), and a runtime
test counts what actually reaches the broker API during refused requests.

---

## 8. The worker keyhole

The deletion worker is a cross-tenant process: it claims an account it is not,
which no tenant session can do. That refusal is correct rather than an obstacle
to route around, so the worker gets a keyhole rather than an exemption — the
same shape as the freshness relay.

* A `NOLOGIN` role, `onyx_privacy_worker`, with `USAGE` on the schema and no
  table grants at all.
* Three `SECURITY DEFINER` functions — claim, advance, fail — each with a pinned
  `search_path`, none taking a table name, a filter or a SQL fragment.
* A claim returns the account id, the state and the cutoff. Not an email, not a
  financial value: the minimum a purge phase needs.
* `FOR UPDATE SKIP LOCKED`, so two workers never fight over the same account.

**Claim recovery.** A claim older than `identity.lifecycle_claim_timeout()`
(10 minutes) is released at the start of the next claim scan and a
`CLAIM_RECOVERED` event is written. A worker that dies mid-phase does not strand
the account; recovery is a property of time, not of cleanup code having run.

**The claim token is the authority.** Advancing or failing an account requires
the token issued with the claim, so a worker whose claim expired and was taken by
another cannot advance the account it no longer holds.

---

## 9. Forgery

A user may start their own deletion and read its status. They may not advance
it, may not complete it, and may not see anyone else's.

* RLS policies for `SELECT` and `INSERT` only. **No `UPDATE` policy and no
  `DELETE` policy exists**, keyed on `app.user_id`.
* The runtime role's privileges are `SELECT, INSERT` — and they are **revoked
  down** to that. `16_rls_grants.sql` sets `ALTER DEFAULT PRIVILEGES` granting
  `UPDATE` and `DELETE` on every new table in the `identity` schema, so a bare
  `GRANT SELECT, INSERT` here would have read like a restriction while changing
  nothing. Without the `REVOKE`, forging progress was blocked only by the absent
  RLS policy — which fails *silently*, at zero rows affected, rather than loudly.
* Lifecycle rows are not deletable: a trigger rejects `DELETE`. Only the
  account's own removal takes the row away, by cascade.
* `identity.account_lifecycle_event` is append-only by trigger and by grant.

Cross-tenant deletion is impossible by construction rather than by check: the
endpoint takes **no account parameter**. It resolves the account from the bearer
token and nowhere else, so there is no field to abuse.

---

## 10. Failure handling

Failures are recorded as **closed codes**, never as exception text. Entry 11A
established why an exception string is privacy-sensitive, and a lifecycle row is
exactly the kind of place they accumulate. Both the column CHECK and the
privileged function enforce the shape `^[A-Z][A-Z0-9_]{2,63}$`.

`SESSION_REVOCATION_FAILED`, `WORKER_CLAIM_LOST`, `PHASE_RETRY_REQUIRED`,
`ACCOUNT_STATE_INVALID`, `LIFECYCLE_CUTOFF_CONFLICT`.

A failed phase moves to `FAILED_RETRYABLE`, releases its claim, and becomes
claimable again. `attempts` increments on every claim. There is no dead-letter
state in 11B1: an account that keeps failing keeps being retried and keeps being
visible as failing, which is the safer of the two wrong answers when the
alternative is quietly stopping.

---

## 11. What is recorded, and what is not

`identity.account_lifecycle_event` is append-only: account id, a closed event
code, from/to states, a worker id, an optional closed reason code, a timestamp.

**No** email address, financial value, document reference, employer name,
personal narrative or exception message — in the table, in the logs, in the
metric labels, or in the API response. The status endpoint returns `status` and
`requested_at` and nothing else; a deleting account learns that it is deleting,
not how the machinery is getting on.

The event table deliberately carries **no foreign key** to `identity.user_account`.
It has to outlive the account it describes, so a restored backup can be told
which accounts were deleted after the snapshot was taken. It is registered in
`MANUALLY_DECLARED_USER_DERIVED` because FK-reachability — how the privacy
inventory derives user-derived tables — cannot see it for exactly that reason.

Metrics are in-process counters with bounded labels, like every other metric in
this repository: `privacy_deletion_requested_total`, `privacy_deletion_phase_total`,
`privacy_deletion_claim_recovered_total`. No `user_id`, no `job_id`, no
`request_id`. There is no exporter, and claiming observability that does not
exist would be the error Entry 11A spent a section on.

---

## 12. Cost

Measured on the test database, three runs of 60 (`scripts/probe_lifecycle_overhead.py`):

| | with cutoff | without |
|---|---|---|
| statements per authenticated request | +2 exactly | — |
| read p50 | +0.70 to +1.15 ms | — |
| write p50 | +0.94 to +1.61 ms | — |
| p95 | −1.0 to +4.2 ms across runs | — |

The p95 band straddles zero: that is run-to-run noise on this machine, not a
measured cost, and is reported as noise rather than as a number. The worker
preflight, which opens its own unit of work, is 2 statements and p50 1.6 ms.

"Without" is the real method replaced by a no-op, so the rows differ by the
check rather than by being different builds.

---

## 13. Backups and restore

Live deletion cannot reach a backup. The contract this entry supports — and does
not implement — is that a restore replays the deletion ledger before resuming
traffic, so a user deleted at T2 does not reappear from a backup taken at T1.

What 11B1 contributes is the ledger's durability properties: the lifecycle row
cannot be deleted, the event log is append-only and survives the account, and
the cutoff is frozen. Restore drills are Entry 11B10.

---

## 14. Other applications

The Node/Express application under `server/` is classified
**`SEPARATE_APPLICATION`** (`app/privacy/classification.py`, `NODE_APPLICATION`).
It shares no code, no database, no identifier and no call with this backend; it
authenticates with its own bcrypt hashes and its own JWTs, and persists its own
users — email, name, password hash, tax profile, documents — to Netlify Blobs.
Root `netlify.toml` deploys it.

**A deletion requested through this backend does nothing to it.** An account
deleted here may still exist there, under the same email address, with a
password hash and uploaded documents. No phase of Entry 11B currently plans to
reconcile the two beyond the note already in the plan (11B9, PD-14).

Whether the deployed instance holds real people's data is an operational fact
about a running site, not a repository fact, and stays flagged as
`OPERATIONAL_REVIEW_REQUIRED`.

---

## 15. Running it

There is **no scheduled lifecycle worker** in Entry 11B1. The claim/advance/fail
interface exists and is tested; nothing calls it on a timer, because the phases
it would drive do not exist yet. Adding a beat schedule now would produce a
worker that walks accounts to `PURGE_PENDING` and stops, which is what the
service already does when driven.

To drive it manually, or from a later phase:

```python
async with unit_of_work(actor_type="system") as session:
    service = AccountLifecycleService(session)
    for claimed in await service.claim(worker_id="privacy-1", batch_size=50):
        ...                                     # do the phase's work
        await service.advance(claimed, LifecycleState.ACCESS_DISABLED,
                              worker_id="privacy-1")
```

`advance` raises `ValueError` if asked for `COMPLETE`.

---

## 16. Known limitations

1. **Nothing is deleted.** `PURGE_PENDING` is as far as an account gets.
2. **PD-1 and PD-4 are both closed** (Entries 11B1 and 11B0). The 11A plan's
   two named prerequisites for the deleting phases are met.
3. **The six scenario routes narrow the in-flight race but do not close it.**
   They take `assert_account_active`, which runs in its own transaction rather
   than the one the handler later opens, so the ordering guarantee in §4.1 does
   not extend to them. Closing it means those routes taking their session from
   `db_authed` like every other route, which is an Entry 9 change.
4. **No cancellation** (`POLICY_DECISION_REQUIRED`).
5. **No grace period** (`PRIVACY_COUNSEL_REVIEW_REQUIRED`).
6. **No scheduled worker** (§15).
7. **`server/` is not reconciled** (§14).
8. **Admission refuses a deleting worker-initiated operation with
   `AdmissionRejected`**, which maps to 429. For HTTP callers `db_authed` fires
   first and returns 403, so this is only reachable from worker-initiated
   admission, where there is no status code and the reason code is what matters.
