# RETENTION / "WHAT CHANGED?" (Entry: Retention / What-Changed Engine)

The deterministic answer to *what materially changed in your governed tax state
since the state you last acknowledged* — and the durable baseline that question
is measured from.

```
GET  /api/v1/ioe/changes?tax_year=YYYY[&as_of=YYYY-MM-DD]      side-effect free
POST /api/v1/ioe/changes/acknowledge?tax_year=YYYY             the only writer
```

```
OpportunityLifecycleService.build_with_assurance(tax_year, as_of)
    ├── TaxAssuranceMap          certified — family standing
    └── OpportunityLifecycleMap  certified — per-opportunity state
            └── snapshot_from_product(…)      pure — the lossy projection
                    └── compare_retention_snapshots(baseline, current)
                            │                 pure — no SQL, no clock
                            └── changes_detail(…)   pure serializer
                                    └── RetentionChangesOut   contract v1.0.0

ioe.retention_checkpoint    the acknowledged baseline, snapshot schema v1.0.0
```

## 1. The baseline is an acknowledgement, never a visit

The authority is **the last state the user explicitly acknowledged** — not the
last login, last request, or last page view. No `last_seen` column exists, and
adding one would not answer this question: a request proves someone loaded
something, not that a person reviewed it.

**A GET never writes.** Refreshing ten times returns the same unacknowledged
changes ten times, and a test asserts exactly that, counting checkpoint rows
afterwards. The alternative — advancing the baseline on read — lets a
background refresh silently erase changes nobody ever saw, which is the single
failure this engine exists to prevent.

## 2. Noise suppression is structural, not a filter

The snapshot is **deliberately lossy**. A field that never enters it can never
produce a change, so the most important part of the design is what it refuses
to record:

| Excluded | Why |
|---|---|
| `source_id` | the optimization candidate's row id — **new on every run**; recording it would make each re-run look like the whole portfolio was replaced |
| `days_remaining` | the daily countdown. 38 → 37 is not news; the **band** transition is |
| `candidate_rank` | the optimizer's presentation order — a shift means some *other* item moved |
| `actionability`, attention flags, `reason_codes` | derived from the axes, so they move exactly when an axis does; including them reports one event twice |
| support scores, `standalone_potential` | figures that drift with inputs the customer never acted on |
| journal thread ids | the Journal is the history authority; retention needs the *decision*, not which thread carried it |
| ordering | every collection is sorted by semantic identity before hashing |

A suppression list applied at *comparison* time is a filter someone eventually
forgets to extend. This one is enforced at the single point where product state
becomes a snapshot.

## 3. Identity — and why it is safe

Opportunities are matched on `opportunity_code`, never on position, label or
amount. Three governed constraints make that sound, and they were verified
rather than assumed:

- `tax_rule_code_key` — rule codes are globally unique;
- `uq_rule_version_published` — at most one published version per rule per tax
  year;
- candidates are deduplicated by `candidate_key` (`code:rule_version_id`).

So the code is **unique within a snapshot** and **stable across observations**.
A rule republished under a new version keeps its code, which is exactly why that
reads as a change to an existing opportunity rather than a removal plus an
addition.

`source_id` — the obvious-looking choice — would have been wrong: it is a
per-run candidate row id.

## 4. Direction, and the fact REMOVED does not claim

Baseline → current. Absent then present is `ADDED`; present then absent is
`REMOVED`; same identity with a material difference is `CHANGED`.

**`REMOVED` is never reported as expiry.** An opportunity that disappeared from
the authoritative current view and one whose governed deadline passed are
different facts with different causes. `EXPIRED` is a timing band the lifecycle
authority assigns, and it is reported as a `TIMING` change on an opportunity
that is still present and still carrying its governed figures.

## 5. Material fields, and one change per axis

`availability` · `decision` · `execution` · `evidence` · `timing` · `freshness`
· `integrity` (with its governed reason) · `deadline_code` / `deadline_date`.

A deadline is compared in its own right because it can move by months and stay
`NORMAL` — the band cannot express that.

Changes are grouped **one per (subject, category)**, so a deadline whose code
and date both moved is one event, while a user who both decided and reported
acting produces two — one per axis that genuinely moved.

Freshness and integrity never collapse into a generic "outdated": they are
separate categories, and a test asserts both survive when they move together.

## 6. Severity is a lookup, not a score

`CRITICAL` (became EXPIRED, integrity failing) · `HIGH` (became URGENT, became
BLOCKED) · `MEDIUM` (approaching, evidence moved, deadline moved, removed) ·
`LOW` (decision answered, action reported, freshness).

Defined as data in one table. **No numeric retention score** — a number invites
sorting by it, sorting invites tuning it, and a tuned number is a recommendation
engine wearing a different hat. Nothing ranks by money; a test greps the modules
to prove they never name an amount field.

Ordering: severity → category → subject → kind. Documented and total.

## 7. Change identity

`change_id = domain_hash(retention_change, {both snapshot hashes, kind,
category, subject, transitions})` — its own registered domain, never a row id.
Binding *both* snapshot hashes means the same transition observed between a
different pair of states is a different change, which is what will let a future
notification path deduplicate deliveries without inventing its own key.

## 8. First use is not a hundred arrivals

With nothing acknowledged, `baseline_status = NO_BASELINE` and the change list
is **empty**. There is no prior authoritative state to have changed from, and
describing a user's existing position as a list of arrivals would report events
that never happened. A missing baseline is not an empty one — against an
explicitly empty baseline, ten opportunities really are ten additions, and a
test asserts both behaviours side by side.

## 9. Acknowledgement: three guards

1. **Idempotency** — `UNIQUE (user_id, request_id)`; a retry returns the
   checkpoint the first attempt created. Checked first, so a network retry never
   trips the staleness guards below.
2. **The state matches** — the server re-derives current state and refuses
   unless the client's `snapshot_hash` is that state's hash → `409` with
   `CURRENT_STATE_CHANGED_SINCE_READ`.
3. **The baseline matches** — the checkpoint being superseded must still be the
   latest → `409` with `BASELINE_ALREADY_SUPERSEDED`.

Guard 3 is enforced **again in the database** by
`uq_retention_checkpoint_chain` — `UNIQUE NULLS NOT DISTINCT (user_id,
tax_year, supersedes_checkpoint_id)`. Two requests can pass a service check
concurrently and only one may land; a service check alone would be a race with
a comfortable name. `NULLS NOT DISTINCT` extends the guarantee to the first
baseline, where the superseded id is `NULL` and Postgres would otherwise admit
both.

## 10. Persistence

One table, `ioe.retention_checkpoint` (migration `0069`). Append-only by
grants — `REVOKE UPDATE, DELETE FROM onyx_app_rw` — following the Decision
Journal's precedent and for the same reason: these rows die through the
`identity.user_account` cascade fired by terminal removal, which does not set
the evidence-purge GUC, so a GUC-gated DELETE trigger would abort the purge it
is meant to permit.

History is preserved: a new acknowledgement **supersedes** its predecessor
rather than rewriting it. "Latest" resolves through a covering index as a single
backwards lookup — measured flat at 1 and 5 checkpoints, so history never taxes
a read.

The snapshot carries its **own** `snapshot_schema_version`, independent of the
read contract's version and of every upstream product version, because these
bytes outlive the code that wrote them. They are read back verbatim and never
reconstructed from current state — a reconstruction would silently lose exactly
the changes worth reporting.

**Measured storage:** ~283 bytes per opportunity. A realistic 10–20 opportunity
user costs ~3–6 KB per checkpoint, ~305 KB at 100 checkpoints. No compression or
blob indirection is warranted.

## 11. Privacy

Classified `LIVE_USER_DATA_DELETE`, `DERIVED`, `WHILE_ACCOUNT_ACTIVE`,
`CASCADE_DELETE`, RLS enforced. Not a replay dependency — nothing verifies
against it, and a lost baseline costs one re-acknowledgement rather than any
sealed guarantee, so there is no conflict between purging it and any certified
replay contract.

The snapshot holds **no** document id, object key, bucket, content hash or
filename; evidence appears only as governed readiness. Both the stored bytes and
the API response are asserted clean against real fixture values.

Both FK edges cascade — `user_id` and the self-referencing
`supersedes_checkpoint_id` — so neither a checkpoint nor a successor can outlive
its parent. The privacy universe widened 72 → 73.

## 12. Measured

- **Zero business authority** on both read and acknowledge: engine `compute` and
  `RulesEvaluatorService.evaluate` counters installed around the fixture build
  first and required to trip there, patching every module in `sys.modules` that
  actually binds `compute`.
- **Checkpoint resolution is O(1)**: 1 checkpoint and 5 checkpoints cost the
  same statement count.
- **Comparator is linear in the sum**: ~34 µs/item flat from 10 to 2000
  opportunities; matching is by key, and the only superlinear step is the sort.
- **Determinism**: repeated requests byte-identical, change identities stable,
  invariant under `PYTHONHASHSEED` 0/1/42.
- **Schema drift 0** on a freshly migrated database.
