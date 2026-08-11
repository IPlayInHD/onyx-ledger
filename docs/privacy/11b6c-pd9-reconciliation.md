# Entry 11B6C — PD-9 reconciliation: the cycle does not exist

The previous slice reported OUTCOME 3, CYCLIC DEPENDENCY, on the grounds that
account removal was refused by a certified PD-9 invariant. **That was wrong**,
and the error was reading a comment as a description of current behaviour.

## What the comment says

`db/sql/44_pd9_durable_deletion_ledger.sql:19`:

    WHAT ACTUALLY HAPPENS TODAY, MEASURED RATHER THAN ASSUMED
    Not silent loss. `DELETE FROM identity.user_account` FAILS:
        ERROR: account lifecycle rows are not deletable

That is the defect PD-9 was written to FIX. It describes the state before
migration 44, not after it.

## What the schema and the database actually do

    identity.account_lifecycle constraints:
      account_lifecycle_pkey  PRIMARY KEY (user_id)
      (no FOREIGN KEY to identity.user_account)

The FK was removed by PD-9 precisely so the ledger could outlive the account.
Measured on a database built from `db/sql` at 0059:

    before   user_account = 1   account_lifecycle = 1
    DELETE FROM identity.user_account ...  -> DELETE 1
    after    user_account = 0   account_lifecycle = 1

Physical account removal works, and the durable deletion ledger survives it.
That is PD-9 working as designed.

## Consequence

MODEL A — physical account removal — is architecturally available today. There
is no PD-9 conflict to reconcile and no cycle to break. What is missing is
ordinary unfinished work, not an architectural blocker:

* no production phase removes the account row;
* `COMPLETE` is still refused unconditionally;
* `DOCUMENTS` and `AUDIT_AUTH_DEIDENTIFICATION` are unimplemented.

## Blast radius for MODEL A, measured

    38 foreign keys reference identity.user_account
       34 ON DELETE CASCADE
        4 ON DELETE SET NULL

Those cascades are what make a bare DELETE dangerous rather than impossible:
the next slice must establish which of the 34 carry evidence that has to
survive (sealed tax history, audit, ledgers) before any phase issues one. The
probe above deleted an account with no dependent rows, so it proves the
lifecycle ledger survives — it does NOT prove a populated account can be
removed safely.

## Status

    Actor Attribution Decision Gate   NOT cyclic; still unrun
    Blocker                           ordinary unimplemented phases
    PD-9                              CLOSED and behaving correctly
