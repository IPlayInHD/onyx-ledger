# PD-4 — audit payload minimization (Entry 11B0)

## What PD-4 was

Entry 11A's gap register (`data-lifecycle-specification.md` §23) recorded it in
one sentence:

> `audit.audit_log` holds whole copies of financial/profile rows, has no user
> FK, is append-only, and account deletion *adds* to it.

`audit.log_change` — one `SECURITY DEFINER` trigger function shared by all 33
audited relations — copied the entire row with `to_jsonb(OLD)` / `to_jsonb(NEW)`
into `audit.audit_log`.

Three properties, each defensible alone:

| Property | Why it exists | Why it is fine on its own |
|---|---|---|
| Whole-row payload | so an auditor can see what changed | audit logs normally do this |
| No foreign key to the account | so the log survives what it describes | append-only logs normally do this |
| Append-only (`trg_audit_immutable`) | so history cannot be rewritten | that is the point of an audit log |

Together they are a second, complete, **unowned and undeletable** copy of every
user's financial and profile data.

## Why Entry 11A classified it as an active defect

`PRIVACY_DEFECT_NOW`, severity HIGH — not an implementation gap. The distinction
in 11A's taxonomy is whether the unsafe thing is *happening*, and it was: every
financial write was adding to the copy.

11A fixed the credential half of it in migration 0046 (**PD-4a**: the Argon2
password hash was being written into the audit log on every registration) and
deliberately left the rest, because deciding what an audit record should contain
touches the Entry 3A governance guarantees and needed its own entry.

## Why it blocks deletion

This is the question §5 of the entry asks, and the answer is specific to this
defect rather than generic.

A deletion phase walks the foreign-key graph out from `identity.user_account`.
`audit.audit_log` has no edge in that graph, so:

1. **The purge cannot reach it.** The account-lifecycle orchestration closed in
   Entry 11B2 has no path to these rows and no later phase built on cascade
   would acquire one.
2. **The inventory could not even see it.** `test_privacy_inventory.py` derives
   the set of user-derived tables by foreign-key reachability. With no edge and
   no manual declaration, the table was absent from the derivation — so the
   guard that exists to stop unclassified user data reaching production was
   structurally unable to flag the largest unclassified store of it.
3. **The rows cannot be removed afterwards.** Append-only by trigger.

So deleting user data before fixing this would have produced an account reported
as **deleted** whose salary, employer name, expense notes and email address were
still queryable — indefinitely, with no mechanism to finish the job. That is
worse than not having offered deletion, because it would have been reported as
done.

## How it was reproduced

Through the production API, not by poking the trigger:
`tests/security/test_audit_payload_privacy.py` registers an account,
authenticates, and POSTs an income record with synthetic markers. Against the
pre-fix trigger the audit payload came back containing:

```
"amount": 134217.73, "source_name": "ZZQX Marker Employer Incorporated"
```

Three tests fail against the previous trigger and pass against the new one. A
fourth was weak — it read the most recent `tax_profile` audit row regardless of
who wrote it, and so passed against the defect by reading somebody else's row.
It is scoped to its own user now.

Registering one account also wrote its email address into the log; the live
test database held 10 such rows before the fix.

## How it was fixed

At the one boundary every audited table already passes through:
`audit.log_change`, in `db/sql/42_audit_payload_minimization.sql` (migration
`0048`). No per-route or per-service filtering.

**Keys stay, values go.** For a personal-data relation each value is replaced
with `[redacted]`, and on an `UPDATE` a column whose value actually moved gets
`[changed]` instead. So the record still answers:

| Question | Answer comes from |
|---|---|
| who | `actor_type`, `actor_id` |
| when | `created_at` |
| what | `entity_schema`, `entity_table`, `entity_id`, `action` |
| which columns existed | the payload's key set |
| which columns moved | `[changed]` |

That is exactly the criterion 11A set: *audit still proves who / what / when, no
financial value persists, Entry 3A four-eyes unaffected.*

**Two allowlists, both deny-by-default.**

`audit.audit_retains_values(schema, table)` — the operator plane keeps whole
payloads: `tax_kb`, `rules`, `admin.rule_change_request`,
`admin.rule_publication`, `tkms`, `ioe.weight_config`. Entry 3A's four-eyes
governance reads what a rule changed *from* and *to*. Everything unlisted is
filtered, so a table added tomorrow is filtered unless someone deliberately
exempts it — and partitions are handled for free, because `TG_TABLE_NAME` is
`income_source_y2025` rather than `income_source`.

`audit.audit_structural_columns()` — the values that survive filtering:
identifiers, lifecycle timestamps, `row_version`, workflow status, and
sealed-evidence content addresses.

**`admin.admin_user` is filtered, not retained.** It is an identity table, not a
governance decision. Four-eyes needs to know *which admin id* approved a change,
which lives in `admin.rule_change_request`, not what their email address is.

## Audit and replay integrity

§8 requires that this not weaken sealed evidence. `manifest_hash`,
`optimization_result_hash`, `optimization_spec_hash`, `scenario_result_hash`,
`scenario_spec_hash`, `baseline_input_snapshot_hash`, `baseline_result_hash` and
the pinned version fields are on the structural allowlist and survive intact.

The reason is the one 11A gave when it refused to let a `%hash%` pattern strip
them: the append-only audit copy is what makes the *live* sealed row
**tamper-evident**. If the live hash were ever altered, the audit copy would
disagree. Removing it to fix a privacy defect would trade one guarantee for
another.

Deny-by-default fails **unsafely** in exactly one direction — a *new* sealed
hash column would default to redacted and quietly erode that evidence. So
`test_a_new_sealed_hash_column_cannot_appear_unlisted` turns that into a build
failure.

## Historical data

| Question | Answer |
|---|---|
| Future writes fixed? | Yes — migration 0048, forward-only. |
| Existing unsafe rows fixed? | **Not automatically.** The log is append-only and forward-only. |
| Mechanism | `scripts/audit_payload_scan.py` — reports first, `--minimize` repairs. |
| Idempotent? | Yes, by predicate: a row already holding markers does not match. |

Not an Alembic data migration, for the reason Entry 11A gave for the credential
scrub: a data migration runs automatically in every environment on upgrade,
including ones with no exposure, and would have to disable the append-only
trigger inside the transaction performing the schema change. An operator runs
this, reads the counts, and decides.

**Output discipline:** counts, timestamps, table names and *column* names. Never
a value. The scan decides by comparing against the marker, so it never has to
print what it found.

**Whether historical exposure exists in a real deployment is
`OPERATIONAL_REVIEW_REQUIRED`.** This repository cannot see a running database.
What it can say is that any environment that served a single financial write
before migration 0048 has it, and that the tool answers the question in seconds.

The repair cannot reconstruct `[changed]`: that needed both sides at write time,
and inventing it afterwards would be inventing evidence.

## Privileges

| Check | Result |
|---|---|
| `PUBLIC` execute on the three functions | revoked; asserted via `has_function_privilege` |
| `onyx_app_rw` on `audit.audit_log` | no SELECT, INSERT, UPDATE or DELETE — asserted by `SET ROLE` and being refused |
| `audit.log_change` owner | `onyx_migrator` |
| `SECURITY DEFINER` | retained — the runtime role has no INSERT on the audit log, so the trigger must run as owner |
| `search_path` | pinned to `audit, pg_catalog` |
| Default privileges | not relied on; the migration is function-only and grants nothing |

Asserted by effective privilege, not by reading the migration. Entry 11B2
learned that the expensive way: an explicit `GRANT SELECT, INSERT` looked
restrictive while broader `UPDATE`/`DELETE` rights survived through
`ALTER DEFAULT PRIVILEGES`.

## PD-1 overlap

```
PD-4 remediation target  ∩  PD-1's 16 tables  =  ∅
audited relations        ∩  PD-1's 16 tables  =  { billing.invoice }
```

The remediation target — `audit.audit_log` and its three partitions — is
disjoint from PD-1. One *audited* relation, `billing.invoice`, is on the PD-1
list, but this entry adds and removes no RLS anywhere, so **Entry 11B1 has
nothing to undo**. PD-1 is **not** closed.

## Two applications

**PD-4 does not apply to `server/`.** The Node application has no change-audit
log. Its `saveAudit(uid, audit)` stores the computed *tax advisory result* on
the user's own record — a different concept that happens to share the word. Its
data model is one record per user, and `deleteUser(uid)` removes that record and
its email index, so it has no unowned duplicate for PD-4 to describe.

`server/` remains `SEPARATE_APPLICATION` (Entry 11B2, §14) and whether its
deployed instance holds real people's data remains
`OPERATIONAL_REVIEW_REQUIRED`. Nothing here changes either.

## Privacy registry

The registry was blind to PD-4's own table. `audit.audit_log` was in `NON_RLS`
with its exception justified, but had **no `LIFECYCLE` entry** and was **not in
`MANUALLY_DECLARED_USER_DERIVED`** — so the derivation could not see it and the
registry that decides what deletion does to user-derived data had nothing to say
about the one table the defect was about.

Both are fixed. It is now classified `AUDIT_SECURITY_RECORD` +
`PSEUDONYMOUS_IDENTIFIER`, `DE_IDENTIFY` on account deletion. Pseudonymous, not
anonymous: after minimization the payload still carries actor and ownership ids,
and 11A was explicit that a UUID or a digest is not anonymity.

## PD-15 — the ownership key is not `actor_id`

Found by the §16 deletion-readiness test rather than assumed.

Registration and login run on an **anonymous** session (`db_anon`, actor type
`system`, no `app.user_id`), so the trigger records **no actor** for the rows
that create an account and its credential. Those rows carry the identity in
their payload instead.

The authoritative ownership key for an audit row is therefore composite:

```sql
actor_id = :uid
OR new_value      ->> 'user_id' = :uid
OR previous_value ->> 'user_id' = :uid
OR (entity_table = 'user_account'
    AND (new_value ->> 'id' = :uid OR previous_value ->> 'id' = :uid))
```

A de-identification phase keyed on `actor_id` alone would leave a deleted user's
account-creation and credential rows behind. Proven to locate everything
carrying the user's id, and proven not to reach the next account.

**Entry 11B3+ inherits this key.** Nothing is deleted or de-identified here.

## Cost

`scripts/probe_audit_trigger_cost.py`, three runs of 300 real inserts with the
two trigger versions swapped on one database:

| | p50 | p95 |
|---|---|---|
| added by minimization | +0.08 to +0.20 ms | +0.04 to +0.27 ms |

Against a ~0.6 ms statement. Statement count is unchanged — the trigger fires
once per row either way and issues the same single INSERT — and the audited row
count is identical, so nothing stopped being audited.

## Tests

`tests/security/test_audit_payload_privacy.py`, 16 tests: the defect through the
production path, the audit record still proving who/what/when, historical
minimization and its idempotency, the operator plane left alone, sealed hashes
preserved, the SQL and Python policies agreeing, a new sealed hash column
failing the build, effective privileges by `SET ROLE`, function ownership and
`search_path`, the composite ownership key, and cross-account exactness.

## Remaining limitations

1. **Historical exposure in any real deployment is unresolved until an operator
   runs the scan.** `OPERATIONAL_REVIEW_REQUIRED`.
2. **`[changed]` cannot be reconstructed** for rows repaired after the fact.
3. **Nothing is de-identified.** PD-4 stops the accumulation; `DE_IDENTIFY` at
   account deletion is Entry 11B3+.
4. **The audit log still has no foreign key** — deliberately. It must outlive
   what it describes, which is why the ownership key exists.
5. **PD-15 is recorded, not implemented.** The key is written down and proven;
   no phase uses it yet.

## Why Entry 11B1 is next

PD-1: 16 tenant-owned child tables have no row-level security, including the
frozen analysis snapshot, document extraction fields, AI messages and AI prompt
context. Until that is closed, deleting data crosses a tenant boundary that is
incomplete — and the 11A plan's ordering rationale was that doing two risky
things at once is the wrong order.

PD-4 was the other half of that rationale. It is closed.
