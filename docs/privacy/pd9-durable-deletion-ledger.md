# PD-9 — the durable deletion ledger (Entry 11B3)

## What PD-9 was

From Entry 11A's gap register (`data-lifecycle-specification.md` §23), verbatim:

> `audit.data_deletion_request` FK is `CASCADE`, so the deletion record dies
> with the account it must outlive.

`IMPLEMENTATION_GAP`, MEDIUM.

## The wording named the wrong table

That sentence was written when `audit.data_deletion_request` was the presumed
home for a deletion ledger. Entry 11A itself described that table as **"EXISTS
AND IS UNUSED"**, and it still holds zero rows.

Entry 11B2 then built the real lifecycle somewhere else —
`identity.account_lifecycle` — and reproduced the same mistake in a stronger
form:

| | `audit.data_deletion_request` | `identity.account_lifecycle` |
|---|---|---|
| Rows | 0 | live, in use |
| Primary key | surrogate `id` | **`user_id`** |
| FK to account | `ON DELETE CASCADE` | `ON DELETE CASCADE` |

Where the unused table merely cascades, the live one **cannot exist without the
account row at all** — the account id is its identity.

The defect is real; its address in the documentation was stale.

## What actually happened, measured

Not silent loss, which is worth recording because it is not what the register
predicted:

```
DELETE FROM identity.user_account WHERE id = ...;

ERROR:  account lifecycle rows are not deletable
CONTEXT: PL/pgSQL function identity.reject_lifecycle_delete() line 3 at RAISE
SQL statement: DELETE FROM ONLY "identity"."account_lifecycle" WHERE ...
```

The cascade tried to destroy the ledger and Entry 11B2's own no-delete trigger
refused it. So the evidence was safe, and **the account could never be removed
while a lifecycle existed** — which blocks the last step of every purge phase
this entry is a prerequisite for. Two wrongs cancelling into a third.

On `audit.data_deletion_request` there is no such trigger, so the cascade there
*would* silently destroy the record. That trap is unarmed only because nothing
writes to the table.

## Why it blocked deletion

The privacy system must not erase its own evidence of deletion:

1. A purge phase that ends by removing the account row would destroy the record
   saying a purge was owed — a half-finished deletion becomes indistinguishable
   from an account that never asked.
2. A backup taken before the deletion and restored afterwards has no surviving
   ledger to replay. The deleted account silently reappears, which Entry 11A
   called the failure that *"undoes every guarantee in this document in one
   operation"*.

## The remediation

**Sever the link, keep the subject.**

```sql
ALTER TABLE identity.account_lifecycle
    DROP CONSTRAINT account_lifecycle_user_id_fkey;
```

`user_id` remains the primary key and *is* the durable subject identifier: the
account's own internal UUID — internal, immutable, never reused, and carried by
the `user_account` rows a restored backup brings back. A ledger keyed on it
identifies exactly which restored accounts are owed a re-deletion.

**What was deliberately not built:**

| Rejected | Why |
|---|---|
| A pseudonymous digest | There is no plaintext to hash, no key to manage and no new cryptography to get wrong. §5 asks that the existing immutable UUID be evaluated first; it is sufficient. |
| A surrogate primary key | `user_id` as the PK is what makes "one logical lifecycle per account" a database fact. Entry 11B2's idempotency — twenty repeated requests, ten concurrent, one row — rests on exactly that. A surrogate key would need a unique constraint saying the same thing. |
| Retained account fields | No email, no name, no status. The ledger keeps a subject identifier and its own machinery, nothing about the person. |

**What replaced the foreign key.** The FK was doing two jobs: preventing rows for
accounts that never existed, and destroying the ledger when the account goes.
The first is worth keeping, the second is the defect. So the check moves to the
only moment it is meaningful — creation — and stops applying afterwards, which
is precisely the asymmetry a foreign key cannot express:

```sql
CREATE TRIGGER trg_account_lifecycle_subject_exists
    BEFORE INSERT ON identity.account_lifecycle
    FOR EACH ROW EXECUTE FUNCTION identity.require_lifecycle_subject_exists();
```

`audit.data_deletion_request` is fixed too rather than left armed — it is the
table a future author would reach for when asked where deletion requests live.
Its **export** sibling is deliberately untouched: an export request has no
reason to outlive the account that asked for it.

## Privacy minimization — every surviving column

The record is retained after the account is gone, so each field has to earn it.

| Column | Why it survives |
|---|---|
| `user_id` | The durable subject. Reconciliation and idempotency both key on it. Pseudonymous, not anonymous. |
| `state` | Which phases are still owed. Without it a restored ledger cannot resume. |
| `requested_at` | The cutoff. Later phases separate data that existed before the request from data written after. |
| `state_changed_at` | Recovery: how long a phase has been where it is. |
| `claimed_by`, `claim_token`, `claimed_at` | Worker recovery. `claimed_by` is a worker identifier, not a person. |
| `attempts`, `last_failure_code` | Retry bookkeeping. The code is a closed enum — never an exception string. |
| `completed_at` | Proves a purge finished, and the CHECK ties it to `COMPLETE`. |
| `revision` | Optimistic concurrency for later phases. |
| `created_at`, `updated_at` | Row lifecycle. |

**Nothing here is a shadow copy of the account.** No email, no name, no
financial value, no document reference, no free-form reason text, no exception
message. `reason` exists on the unused `audit.data_deletion_request` and stays
unused.

Could anything be dropped after `COMPLETE`? `claim_token` and `claimed_by` have
no meaning once the lifecycle is finished, and shedding them is a reasonable
future minimization — but `COMPLETE` is unreachable until a purge phase exists,
so there is nothing to shed yet.

## Security

| Control | Result |
|---|---|
| RLS | `ENABLE` + `FORCE`, policies keyed on `app.user_id`, unchanged |
| After account removal | No session can present that id again, so the row becomes invisible to every ordinary role **by construction** — no policy change needed |
| `onyx_app_rw` | SELECT, INSERT. **No DELETE, no UPDATE** — cannot forge progress, complete a lifecycle, move a subject, or delete the record |
| `onyx_app_ro` | SELECT only; INSERT/UPDATE/DELETE all absent |
| `PUBLIC` | Nothing |
| Worker | The Entry 11B2 keyhole — three `SECURITY DEFINER` functions, pinned `search_path`. Unchanged, and no new grant |
| Deletion | Refused twice: the privilege is absent, and the no-delete trigger refuses even a role that has it |

All asserted by `SET ROLE` and being refused, never by reading DDL and never by
a zero-row result.

## Worker recovery without a live account

The critical property. None of the keyhole functions ever joined
`identity.user_account`, so all of them keep working with no account row:

* `claim_account_lifecycle` claims a subject whose account is gone;
* `advance_account_lifecycle` and `fail_account_lifecycle` operate on the claim
  token;
* `account_deletion_state` — used by login, admission and the worker preflight —
  still answers `DELETION_REQUESTED` for a removed subject, so a restored
  backup's account is not treated as active.

Claim semantics are untouched: 10-minute expiry, single active claimant,
`CLAIM_RECOVERED` events.

## No resurrection linkage

Registering the same email again produces a **new** internal UUID, so it cannot
pick up the previous subject's ledger. Two subjects, two ledgers, proven — and a
ledger keyed on the email address would have done exactly the wrong thing.

## Backup and PITR readiness

PD-9 establishes the **storage foundation** and nothing more. What a future
process can now do that it could not before:

* query `identity.account_lifecycle` for subjects whose `requested_at` falls
  after a restore point, and
* replay those deletions against the restored data,

because the records survive the account rows they describe.

**Backup reconciliation is not implemented and is not claimed.** No restore
drill, no PITR configuration, no automatic re-deletion. That remains the future
entry Entry 11A's §30 describes.

## PD-15 interaction

Entry 11B0 recorded that the audit log's ownership key is not `actor_id` alone.
The durable subject here is the same value that key's first branch uses, so a
future de-identification phase can use it to locate audit rows for a subject
whose account is gone — which is otherwise hard, because `actor_id` is NULL for
the anonymous registration and login events.

That is a **possibility recorded, not a fix**. PD-15 remains open, and the
deletion ledger's subject identifier is not the same concept as audit ownership.

## PD-3

Untouched. PD-9's FK redesign forced no decision about `identity.login_event` or
the other `SET NULL` de-identification targets. **PD-3 remains open.**

## Existing data

Constraint-only. No column added, no row written, no value recomputed. Proven on
a database migrated to `0049` first:

| | Result |
|---|---|
| Lifecycle rows seeded pre-migration | 2 (one advanced to `ACCESS_DISABLED`, revision 2) |
| After `upgrade head` | **identical** — `requested_at`, `state_changed_at`, `state`, `revision`, `attempts` all unchanged |
| Account removal afterwards | succeeds |
| Ledger rows for the removed account | 1 |

That is why this is a constraint change rather than a backfill: a migration that
recreated lifecycle rows could reset a cutoff or duplicate a request event, and
this one cannot.

## Downgrade

Restores the cascade and therefore reinstates PD-9. Worse, it **fails outright**
if any ledger row has already outlived its account — there is nothing for the
foreign key to point at, which is precisely the state the upgrade makes
possible. Development and test environments only; a production downgrade past
this revision is not a supported operation.

The security-gate proof demonstrates this: its cascade case has to be added
`NOT VALID`, because by that point the database holds records that have outlived
their accounts.

## Failure injection

Six PD-9 cases in `scripts/prove_security_gate.sh`, all rejected:

| Restored defect | Caught by |
|---|---|
| cascade on the ledger | the account becomes undeletable |
| cascade on `audit.data_deletion_request` | the constraint test |
| `DELETE` granted to the runtime role | effective-privilege test |
| the subject's primary key dropped | duplicate-subject test |
| worker state lookup requires a live account | removed-account lookup returns NULL |
| creation-time integrity trigger dropped | orphan creation succeeds |

## Cost

| Lifecycle INSERT, n=200 | p50 | p95 |
|---|---|---|
| before (foreign key) | 0.392 ms | 0.601 ms |
| after (trigger) | 0.330 ms | 0.569 ms |

Marginally faster, and within noise either way — the trigger does the same
lookup the foreign key did. Statement count unchanged: one INSERT.

The change that matters is not a latency one. Removing an account went from
*impossible* to *possible*.

## Remaining limitations

1. **Nothing is deleted.** This is a storage prerequisite.
2. **Backup reconciliation is not implemented** (§10 above).
3. **`COMPLETE` is still unreachable** — no purge phase exists to earn it.
4. **Post-`COMPLETE` minimization is not implemented**: `claim_token` and
   `claimed_by` could be shed once a lifecycle finishes.
5. **PD-3 and PD-15 remain open**, untouched here.
