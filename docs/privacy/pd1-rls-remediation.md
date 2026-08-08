# PD-1 — tenant RLS for the 16 child tables (Entry 11B1)

## What PD-1 was

From Entry 11A's gap register (`data-lifecycle-specification.md` §23), verbatim:

> 16 tenant-owned child tables have no RLS, including the frozen snapshot,
> extraction fields, AI messages and AI prompt context. (27 user-derived tables
> lack RLS in total; 11 of those — `identity` and `audit` — correctly cannot
> have it.)

`IMPLEMENTATION_GAP`, HIGH.

Entry 11A was careful about what it was and was not claiming: *"This is not a
demonstrated leak. Every service scopes through the parent, and no API path
reaches these tables unscoped."* The defect was that the protection was every
service **remembering**, in a repository that elsewhere states RLS is "the
tenant-correctness boundary, not the query access path".

## The 16 tables and their ownership

None has a `user_id` of its own. Every one reaches its owner through a **NOT
NULL** parent foreign key.

| # | Table | Owner path | Shape |
|---|---|---|---|
| 1 | `ai.ai_message` | `ai_conversation.user_id` | depth-1 |
| 2 | `ai.ai_message_citation` | `ai_message` → `ai_conversation.user_id` | depth-2 |
| 3 | `ai.ai_prompt_context` | `ai_message` → `ai_conversation.user_id` | depth-2 |
| 4 | `analysis.analysis_assumption` | `analysis_run.user_id` | depth-1 |
| 5 | `analysis.analysis_input_snapshot` | `analysis_run.user_id` | depth-1 |
| 6 | `analysis.analysis_line_item` | `analysis_run.user_id` | depth-1 |
| 7 | `analysis.reconciliation_check` | `analysis_run.user_id` | depth-1 |
| 8 | `billing.invoice` | `subscription.user_id` | depth-1 |
| 9 | `docs.document_extraction` | `document.user_id` | depth-1 |
| 10 | `docs.document_link` | `document.user_id` | depth-1 |
| 11 | `docs.extraction_field` | `document_extraction` → `document.user_id` | depth-2 |
| 12 | `ioe.run_rule_snapshot` | `optimization_run.user_id` **or** `scenario.user_id` | two-branch |
| 13 | `reco.recommendation_status_event` | `recommendation.user_id` | depth-1 |
| 14 | `wealth.asset_valuation` | `asset.user_id` | depth-1 |
| 15 | `wealth.liability_balance` | `liability.user_id` | depth-1 |
| 16 | `wealth.registered_account_detail` | `asset.user_id` | depth-1 |

Ownership was derived from the live schema, not assumed. **Two traps found by
doing so:**

`reco.recommendation_status_event` also has an `actor_user_id` — a `SET NULL`
FK to `identity.user_account`. That is **whoever changed the status**, which may
be an operator. Keying the policy on it would have manufactured tenant ownership
from a loosely related column and produced a policy that is wrong in exactly the
cases that matter.

`ioe.run_rule_snapshot` carries `CHECK (num_nonnulls(run_id, scenario_id) = 1)`.
A single-branch policy would have denied every row of the other kind — an outage
rather than isolation, and one that reads as working if you only test one branch.

## Why it blocked deletion

Not "a table has no RLS". The question the entry asks is whether an ordinary
role, worker, query or **future privacy deletion path** could reach another
user's rows because the boundary is not enforced by the database.

The later purge phases resolve what belongs to an account and then act on it.
Doing that across a boundary the database does not enforce means one mistake
deletes or exposes another tenant's frozen snapshot, extraction fields, AI
messages or invoices — and the failure is silent in the direction nobody checks,
because a purge that removes too much looks like it worked.

## Pre-fix proof

`tests/security/test_pd1_tenant_isolation.py` does not go through the API,
deliberately: an API test proves the service remembered to scope its query,
which is the property PD-1 says to stop relying on. It becomes `onyx_app_rw` via
`SET ROLE`, sets `app.user_id` the way `unit_of_work` does, and issues SQL at
the table.

Against the previous schema, **81 failures**:

| Count | What tenant A could do to tenant B |
|---|---|
| 16 | read B's rows |
| 16 | update B's rows |
| 16 | delete B's rows |
| 16 | insert rows into B's tree |
| 16 | read everything with *no* tenant context at all |
| 1 | move a child row into B's tree |

With the policies: **98 pass**. Removing them returns the 81.

## The policies

```sql
ALTER TABLE <child> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <child> FORCE ROW LEVEL SECURITY;
CREATE POLICY p_self_<child> ON <child>
    FOR ALL
    USING      (EXISTS (SELECT 1 FROM <parent> p
                         WHERE p.id = <child>.<fk>
                           AND p.user_id = ref.current_app_user()))
    WITH CHECK (<the same predicate>);
```

**Resolved to `user_id` explicitly**, not chained through the parent's own RLS.
Chaining works — a parent policy does apply inside a child's `EXISTS` — but it
makes each child's isolation depend on a policy on a different table, and that
dependency is invisible when reading the child. This is the idiom Entry 3B
already used for the 18 `ioe` children.

**A denormalized `user_id` on each child was considered and rejected.** It would
put a writable ownership column on sealed evidence (`analysis_input_snapshot`,
`run_rule_snapshot`), creating a way to *change* ownership that does not exist
today, and would need a backfill plus a trigger to keep it true. The parent
pointer is already NOT NULL and already immutable in practice.

**`FOR ALL` with both `USING` and `WITH CHECK`.** A `USING`-only policy protects
reads and leaves ownership forgery wide open — inserting into another tenant's
tree, or moving a child there by rewriting its parent pointer. Both were
reproduced before the fix and both are tested after it.

**Deny-by-default falls out of the predicate.** `ref.current_app_user()` returns
NULL when `app.user_id` is unset — a login, a system worker — and `NULL =
anything` is never true, so an anonymous session matches no rows without a rule
saying so.

## Effective privileges

Asserted by `SET ROLE` and being allowed or refused. Never by reading DDL, and
never by observing zero affected rows — that is what a *policy* does, not what a
missing privilege does.

| Role | SELECT | INSERT | UPDATE | DELETE | TRUNCATE |
|---|---|---|---|---|---|
| `onyx_app_rw` | ✅ | ✅ | ✅ | ✅ | ❌ |
| `onyx_app_ro` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `onyx_freshness_worker` | ❌ | ❌ | ❌ | ❌ | ❌ |
| `onyx_privacy_worker` | ❌ | ❌ | ❌ | ❌ | ❌ |
| `PUBLIC` | ❌ | ❌ | ❌ | ❌ | ❌ |

**Grants are deliberately unchanged.** `onyx_app_rw` keeps CRUD, matching every
one of these tables' parents. RLS decides *which rows*; the grant decides
*whether the command exists*. Removing a privilege the product uses today
because a future privacy worker will want its own boundary is not this entry's
job — that worker gets its own keyhole when the purge is written.

`TRUNCATE` is confirmed absent, because a policy cannot filter a whole-table
wipe.

`onyx_app_ro` is read-only **and** still subject to the policies: `FORCE` means
a reporting role cannot read across tenants either.

## Default privileges

`ALTER DEFAULT PRIVILEGES` grants `onyx_app_rw` full CRUD (`arwd`) in every
tenant schema, and `onyx_app_ro` SELECT in `ioe`. That is correct and is **not**
changed — the runtime role writes user data.

What the default does *not* do is give a new table a policy. **That is exactly
how PD-1 happened, sixteen times.** So the default is recorded rather than
removed, and the regression guard is the invariant instead:

> any table carrying a `user_id`, in a tenant schema, must have RLS enabled,
> FORCED, and at least one policy — unless it is justified in the non-RLS
> registry.

Proven, not asserted: `scripts/prove_security_gate.sh` creates a tenant-owned
table with CRUD and no boundary and the suite rejects it.

## Failure injection

`scripts/prove_security_gate.sh` — 9 cases, all rejected:

| Removed | Caught by |
|---|---|
| a policy (`analysis_input_snapshot`) | isolation tests |
| `ENABLE RLS` (`ai_message`) | read-only role sees rows |
| `FORCE RLS` (`extraction_field`) | registry/database RLS mismatch |
| `WITH CHECK` → USING-only (`invoice`) | policy-shape invariant |
| `UPDATE` restored to `onyx_app_ro` | effective-privilege test |
| a new unguarded tenant table | the regression guard above |
| (plus the 3 pre-existing cases) | |

The `WITH CHECK` case matters most: reads stay correct and ownership forgery
reopens, which a read-only suite would never catch.

## Partitions

**None of the 16 is partitioned.** Recorded as a test rather than an assumption,
because parent RLS does not protect child partitions in PostgreSQL and this
repository has already fixed one real bypass of that kind. If one of these ever
becomes partitioned, that test is where it gets noticed.

`docs.document_link` has a nullable FK *to* the partitioned
`finance.expense_record`, which already carries RLS on the parent and every
partition. Its own ownership runs through `document_id`.

## Billing and audit together

`billing.invoice` is the only table in both the audited set and the PD-1 set —
found in Entry 11B0. Both controls hold: B's invoice is unreachable from A's
session, and the audit row still names its columns while carrying `[redacted]`
for the amount. The ownership pointer is deliberately **not** redacted, because
a future de-identification phase needs it to attribute the row.

## Existing data

RLS is a visibility change, not a data transformation. Nothing was rewritten.
Safe aggregate checks before enabling, counts only:

| Check | Count |
|---|---|
| orphaned children (all 15 depth-1/2 chains) | 0 |
| `run_rule_snapshot` resolving to no owner | 0 |
| parents with NULL `user_id` | 0 |

Zero is the expected answer — every one of these FKs is enforced and NOT NULL.
The checks are kept as tests so a future schema change that weakens one is
noticed here rather than during a purge.

## Performance

`scripts/probe_pd1_rls_cost.py`, two runs of 200 with 25 children per parent:

| Query shape | Δ p50 |
|---|---|
| depth-1 (`analysis_line_item`) | +0.09 to +0.19 ms |
| depth-2 (`extraction_field`) | +0.36 to +0.39 ms |
| two-branch (`run_rule_snapshot`) | +0.18 to +0.26 ms |
| unscoped child scan (`ai_message`) | +0.10 to +0.22 ms |

on sub-millisecond queries. Depth-2 is consistently the most expensive, which is
what two joins inside a correlated `EXISTS` should cost.

Eight indexes were added on the **child** side of each predicate; the parent
side is a primary-key lookup. Every parent already carries an index on the
column its policy filters — checked rather than assumed, since a correlated
`EXISTS` with no index on either side is how these become a hot-path problem.

## Account lifecycle

Entry 11B2 remains closed and is exercised after the policies: reads traversing
the newly protected children work while the account is active and return 403
once the cutoff lands — for the lifecycle's reason, not an RLS one.

The privacy worker gained **no** access to any of these tables. Purge authority
belongs to the entry that writes the purge.

## PD-15

Entry 11B0 recorded that the audit log's ownership key is not `actor_id` alone.
Nothing here changes that, and nothing here assumes otherwise: none of the 16
policies references `audit.audit_log`, and the de-identification phase that will
need the composite key is still ahead.

## Two applications

PD-1 is a PostgreSQL row-level-security defect. `server/` (Node/Netlify Blobs)
has no PostgreSQL and no RLS; its isolation model is one record per user in a
key-value store. **PD-1 does not apply to it.** It remains
`SEPARATE_APPLICATION` with its deployment state `OPERATIONAL_REVIEW_REQUIRED`.

## Registries

Updated **after** the implementation was proven, not to make tests pass:

* the 16 are **removed** from the non-RLS registry entirely — not relabelled,
  because they are no longer non-RLS tables;
* the lifecycle registry records `rls=True` for all 16, and a test compares that
  flag against the live database;
* the Entry 11A guard asserting *"the PD-1 set is exactly 16 tables"* was correct
  while the gap was open and would have failed the moment it closed. It now
  asserts there is **no** such defect, plus the sixteen by name.

## Non-RLS inventory

| | Before | After |
|---|---|---|
| non-RLS registry entries | 100 | 84 |
| PD-1 defect entries | 16 | **0** |
| unexplained privacy-relevant non-RLS | 0 | **0** |

The remaining 84 are the legitimate classes Entry 11A justified: `identity`
authentication bootstrap, `audit` operator stores, `admission` global counters,
and the non-tenant schemas. "Zero non-RLS tables" was never the goal.

## Remaining limitations

1. **Nothing is deleted.** This is a security prerequisite, not a purge.
2. **Deletion still needs its own boundary.** The privacy worker has no access
   to these tables and should get a keyhole, not a blanket grant.
3. **Downgrading migration 0049 reinstates PD-1** for as long as it is down.
   Recorded in the migration; test environments only.
4. **Depth-2 policies cost roughly twice depth-1.** Acceptable at current
   shapes; worth re-measuring if `extraction_field` becomes a hot read path.
