# Entry 11B6B — DESIGN_BLOCKER: deleted-customer actor attribution

Found while wiring `AUDIT_AUTH_DEIDENTIFICATION` into the deletion lifecycle.
The phase was **not** wired, because wiring it would produce a phase that
reports attribution removed while a single join restores it.

## What was measured

On a database built from `db/sql` at revision `0058_audit_writer_subject`, with
a customer A who performed a self-action (`actor = A`, `subject = A`) and an
operator action on A (`actor = operator`, `subject = A`), after severing A's
subject exactly as `identity.deidentify_audit_auth` does:

    subject still resolves ............................ false   (as designed)
    audit rows still carrying A's UUID in actor_id .... 1
    re-identification join path ....................... FOUND

The join:

```sql
SELECT a.actor_id, d.subject_key
  FROM audit.audit_log a
  JOIN identity.deletion_subject d ON d.user_id = a.actor_id;
```

returns A's severed `subject_key`. One join, no privileged access beyond
reading two tables, and the severed correlation is back.

## The two defects, which are not the same

**1. `deletion_subject` retains the live account UUID.** This is mine, from
Entry 11B6. The tombstone keeps `user_id` so a retry finds the existing key
instead of minting a second one — real idempotency value — but it also gives
anything holding A's old UUID a way back to A's retained history. This one is
fixable in design: the mapping does not need to survive the phase in a form
keyed by the live id.

**2. `audit.audit_log.actor_id` permanently retains the deleted customer's
live account UUID.** This one is not fixable the same way. The rows are
append-only by deliberate design — `reject_mutation` refuses UPDATE even for
the table owner — and the rows already exist. Nothing can rewrite them, and
nothing should be able to.

Entry 11B6 solved the SUBJECT problem with indirection: audit rows carry a
`subject_key`, and attribution is severed by removing what the key resolves to.
The ACTOR problem has exactly the same shape and would need exactly the same
answer — an `actor_key` indirection — but `actor_id` is a direct UUID and the
history predates any such column.

## Why this blocks the phase rather than being a footnote

PD-15 asks that a deleted subject "no longer remains unnecessarily
attributable". A stable identifier for the person survives in immutable
history, and a live table maps it to the retained subject key. A phase that
completed under those conditions would be asserting something the database can
contradict in one query — the exact class of status this codebase refuses
elsewhere (`advance` refuses `COMPLETE`; the SOURCE_DATA phase refuses to
complete while rows remain).

So the phase is not wired and `AUDIT_AUTH_DEIDENTIFICATION` remains unreached
by the worker.

## What is NOT claimed

* No claim that a person's email or profile is recoverable. After full
  deletion `identity.user_account` is gone, so `actor_id` resolves to no
  contact detail inside the live database.
* No claim that this is exploitable by an ordinary application role;
  `deletion_subject` and `account_subject` grant nothing to `onyx_app_rw` or
  `onyx_app_ro`.
* The severity is that a **durable stable correlator for a deleted person
  persists**, and the mapping that was supposed to be one-way is not.

## Options for the next slice, none of them started

1. **Actor indirection.** Add `audit.audit_log.actor_key` resolved through a
   mapping, mirroring the subject design, and stop writing `actor_id` for
   customer actors. Does nothing for rows already written.
2. **Break the join only.** Stop `deletion_subject` from retaining `user_id` —
   for example key the tombstone by `subject_key` alone and make idempotency
   depend on the phase row rather than the mapping. Removes defect 1 and
   leaves defect 2.
3. **Accept and document.** Decide that a deleted customer's own account UUID
   in immutable audit history is retained security evidence. That is a policy
   position, not an engineering one, and would need
   `PRIVACY_COUNSEL_REVIEW_REQUIRED`.

Option 2 is cheap and strictly an improvement. Option 1 is the real fix for
new rows. Neither was implemented here, because choosing between them —
particularly whether option 3 is acceptable — is a decision this entry does
not have the authority to make alone.

## Status

    11B6B lifecycle wiring    BLOCKED
    PD-15                     OPEN
    classification            DESIGN_DEFECT (architecture gap)
                              + POLICY_DECISION_REQUIRED for option 3
