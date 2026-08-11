# RETAINED ROOT FK MODEL — how the account row leaves without taking evidence with it

Four direct `ON DELETE CASCADE` foreign keys stand between `identity.user_account`
and evidence proven to require survival. This decides exactly how each should
change. No migration is written here.

The answer is the same for all four — **`R1_DROP_FK_PRESERVE_UUID`** — plus a
state-conditioned cleanup phase for `ioe.scenario`. Every alternative was
eliminated by measurement, not preference.

## The distinction the whole analysis turns on

The foreign key and the stored value are separate things. `user_id` can keep
pointing at an account that no longer exists; what cannot survive is the
*constraint* insisting it does. Severing referential dependency does not require
erasing the historical UUID, and preserving the UUID is what keeps sealed bytes
byte-identical.

## Current behaviour

```
DELETE FROM identity.user_account   →   REFUSED
reason: ioe.integrity_check is append-only (DELETE rejected)
```

The refusal holds inside `app.allow_evidence_purge = 'on'` as well:
`ioe.guard_integrity_check_transition` has no escape clause, unlike
`ioe.reject_result_mutation`. Terminal account deletion is not merely
destructive — it is **not executable**.

## The end-to-end model, measured

A disposable database (`onyx_fkmodel`), schema applied from `backend/db/sql`, one
account built entirely through production services: analysis → optimization →
scenario → two verifications → `identity.purge_source_data`.

| step | result |
|---|---|
| terminal delete, before the model change | REFUSED — `integrity_check is append-only` |
| `ALTER TABLE … DROP CONSTRAINT` ×4 | sealed bytes unchanged by the DDL |
| terminal delete, after | **SUCCEEDED**, 1 row |
| `analysis_run`, `optimization_run`, `scenario`, `integrity_check`, `run_rule_snapshot`, `analysis_input_snapshot`, `scenario_result` | all **survive** |
| sealed bytes after deletion | **byte-identical** |
| `verify("optimization")` with the account gone | `verified`, reason `NONE` |
| `verify("scenario")` with the account gone | `verified`, reason `NONE` |

Replay works because nothing in the replay stack references the account:
`resolver.py`, `verification.py`, `scheduler.py` and `services.py` contain no
mention of `user_account`, and the resolver reads the frozen snapshot rather
than the live financial tables.

## Why not the alternatives

**R2 — `ON DELETE SET NULL`. Impossible on all four.** Every root's `user_id` is
`NOT NULL`; a `SET NULL` cascade raises rather than degrading. Adopting it would
first require dropping `NOT NULL` on retained evidence, weakening the ownership
column to buy nothing R1 does not already give.

```
analysis.analysis_run   SET NULL REFUSED  null value in column "user_id" …
ioe.optimization_run    SET NULL REFUSED  null value in column "user_id" …
ioe.scenario            SET NULL REFUSED  column "user_id" is sealed once completed
ioe.integrity_check     SET NULL REFUSED  row is already terminal
```

**R3 — subject-key rewrite. Impossible on two of four.** `ioe.scenario` lists
`user_id` among its sealed columns; `ioe.integrity_check` refuses every UPDATE
once terminal. Only `analysis_run` and `optimization_run` would permit it, and a
model that rewrites two roots while leaving two untouched is worse than one that
touches none.

**R4 — sidecar.** No measured access need requires it. RLS already resolves
through the GUC, and the governed reader works unchanged after deletion.

**`NO ACTION` is not severance** (§27). Proven, not assumed: with a `NO ACTION`
child row present, the parent delete is refused —
`violates foreign key constraint`. It converts an automatic child delete into a
blocked parent delete, which is the same dead end the append-only guard already
produces. Only removing the child FK lets the account row go.

## Per-root models

### `analysis.analysis_run` → `R1_DROP_FK_PRESERVE_UUID`
Row mutation 0 · hash impact 0 · replay PASS · RLS unchanged · no ORM cascade.
The only root whose `user_id` is technically writable (no immutability trigger),
which is precisely why the model that writes nothing is preferable.

### `ioe.optimization_run` → `R1_DROP_FK_PRESERVE_UUID`
Row mutation 0 · hash impact 0 · replay PASS. `user_id` is not in its sealed
column list, so a rewrite would be permitted — and is still not taken.

### `ioe.run_rule_snapshot` (depth 2) — preservation proof
Not a direct child, and its FK is untouched. With `optimization_run` retained,
the rule pin survives with `snapshot_hash` unchanged and replay verifies. Its
RLS is a **parent-object join** (through `optimization_run` OR `ioe.scenario`),
so retaining the parents is exactly what keeps it reachable. No descendant FK
changes.

### `ioe.integrity_check` → `R1_DROP_FK_PRESERVE_UUID`
Append-only: DELETE rejected unconditionally, UPDATE rejected once terminal, and
only `SELECT, INSERT, UPDATE` granted. `user_id` is immutable evidence.
Dropping the FK is the *only* model available, and it is also the one that
removes the blocker: this guard is what currently refuses the terminal delete.

Future verification after deletion is technically unaffected — a post-deletion
`verify()` succeeded and appended new rows for the deleted subject. Whether it
*should* continue is **POLICY_DECISION_REQUIRED**; both answers leave the
existing history structurally valid.

### `ioe.scenario` → `R5_STATE_CONDITIONED` (R1 on the FK **plus** a cleanup phase)

R1 alone is **not sufficient here**, and this is the one place the analysis
changed the answer. Measured: with the FK dropped and the account deleted, an
unsealed `pending` scenario carrying `label = 'UNSEALED LIVE LABEL'` **also
survived** — accidental retention of live product state, exactly what §15 warns
against.

The table supports the fix directly:

| operation on a **completed** scenario | result |
|---|---|
| `UPDATE label` | ALLOWED |
| `UPDATE note` | ALLOWED |
| `UPDATE scenario_result_hash` | REFUSED — sealed |
| `UPDATE user_id` | REFUSED — sealed |
| explicit `DELETE` of an **unsealed** row in the purge context | SUCCEEDS |

So the model is: drop the FK for the sealed rows, and add a lifecycle phase that
before account removal (a) deletes unsealed scenarios and (b) clears `label` and
`note` on retained ones. The schema comment confirms the intent —
*"Label, note, visibility, freshness and supersession deliberately REMAIN
writable"* — so `DEIDENTIFY_THEN_RETAIN` has a real mechanism and is not an
aspiration.

## Historical UUID privacy

With the account row gone, A's UUID still reaches:

```
analysis.analysis_run              ioe.optimization_run
ioe.scenario                       ioe.integrity_check
identity.account_lifecycle         identity.account_lifecycle_phase
identity.account_subject
```

and resolves to **nothing personal**: email 0, profile 0, tax profile 0, income
0, expenses 0, documents 0, credentials 0.

**One ordering constraint falls out of this.** `identity.account_subject` — the
live map from account to audit subject key — was still present, because only the
SOURCE_DATA phase had run. While it exists, the historical UUID still resolves
audit rows to a subject. That is the open PD-15 work, not a new defect, but it
fixes an ordering requirement: **AUDIT_AUTH_DEIDENTIFICATION must run before or
with account removal**, or audit attribution outlives the account.

## Same-email re-registration

A deleted; B registered with A's former email through the normal path.
`B UUID != A UUID`, and B sees **0 rows** of every retained root. Email equality
creates no bridge, because ownership resolves through the UUID and nothing
reconciles the two.

## RLS

| root | policy form | after account removal |
|---|---|---|
| `analysis.analysis_run` | `DIRECT_USER_GUC` | no GUC → 0 rows; historical subject → resolves |
| `ioe.optimization_run` | `DIRECT_USER_GUC` | same |
| `ioe.scenario` | `DIRECT_USER_GUC` | same |
| `ioe.integrity_check` | `DIRECT_USER_GUC` | same |
| `ioe.run_rule_snapshot` | `PARENT_OBJECT_JOIN` (optimization_run OR scenario) | resolves while parents retained |

`ref.current_app_user()` reads the `app.user_id` setting and nothing else — **no
policy contains `EXISTS (SELECT 1 FROM identity.user_account …)`**. This is why
§20's failure mode does not arise: deleting the account neither grants access
nor withdraws it. Measured from a genuine `onyx_test` login against the modeled
database: no GUC → 0 rows on all five; historical subject → rows resolve.

## ORM cascades

**None exist.** `grep -c "relationship("` across `app/` and `workers/` returns
**0** — the models are plain column mappings throughout. No SQLAlchemy cascade
can delete a retained root after the database FK stops doing so. Pinned by
`test_no_orm_relationship_can_delete_a_retained_root`.

## Triggers

| root | triggers | interaction with the model |
|---|---|---|
| `analysis.analysis_run` | `trg_audit` | none — no rows touched |
| `ioe.optimization_run` | `trg_audit`, `trg_guard_transition`, `trg_set_updated_at` | none |
| `ioe.scenario` | `trg_audit`, `trg_guard_transition`, `trg_set_updated_at` | cleanup phase writes only unsealed columns |
| `ioe.integrity_check` | `trg_integrity_check_append_only` | none — and it stops refusing once the cascade no longer reaches it |
| `ioe.run_rule_snapshot` | `trg_immutable` | none |

No bypass is required anywhere. Every protection trigger stays active.

## Candidate 0060

```
analysis.analysis_run   analysis_run_user_id_fkey
  current: FOREIGN KEY (user_id) REFERENCES identity.user_account(id) ON DELETE CASCADE
  target:  DROP CONSTRAINT

ioe.optimization_run    optimization_run_user_id_fkey     (same shape)
ioe.scenario            scenario_user_id_fkey             (same shape)
ioe.integrity_check     integrity_check_user_id_fkey      (same shape)
```

`DROP CONSTRAINT`, not `NO ACTION` — a surviving row referencing a deleted
parent cannot satisfy any normal foreign key.

**Historical row mutations required: sealed UPDATEs = 0, sealed DELETEs = 0.**
Target met. The scenario cleanup touches unsealed live rows and unsealed columns
only, which is not sealed-history mutation.

**Indexes are kept.** `ix_analysis_user_year`, `ix_ioe_run_user_year`,
`ix_ioe_scenario_user`, `ix_ioe_integrity_check_user`, plus the `user_id`-bearing
unique constraints `uq_ioe_run_idempotency`, `uq_ioe_run_spec_current`,
`uq_ioe_scenario_idempotency`, `uq_ioe_scenario_spec`. None of them exists to
back the foreign key; they serve the RLS predicate and idempotency, both of which
outlive it.

## Locking

`ALTER TABLE … DROP CONSTRAINT` takes **`AccessExclusiveLock` on both the child
and `identity.user_account`** (measured via `pg_locks`). It is a catalog-only
change — no table scan, no row rewrite, so the lock is held briefly — but four
drops in one transaction hold an exclusive lock on the account table for the
duration. `NOT VALID` / `VALIDATE` are irrelevant: those apply to adding
constraints, and nothing is added.

## Downgrade

**A faithful downgrade becomes impossible once the feature is used.** Re-adding
`FOREIGN KEY (user_id) REFERENCES identity.user_account(id)` fails as soon as any
retained row references a deleted account, and the only ways to make it succeed
are to delete the evidence or resurrect the accounts. Neither is acceptable.

0060 should therefore refuse to downgrade explicitly rather than offer a
downgrade that quietly destroys evidence to satisfy the old constraint. Recorded
now so the migration is written that way from the start.

## Out of scope, deliberately

Backup/PITR reconciliation — a restored account row plus a retained historical
UUID would recreate attribution. That is future work and is not a reason to
reject an otherwise sound live-database model.

## State

Registry unchanged: 70 total, 7 classified, 63 `UNCLASSIFIED_BLOCKING`. Terminal
readiness remains **BLOCKED**, and must. This slice makes 0060 designable; it
does not make deletion launchable.
