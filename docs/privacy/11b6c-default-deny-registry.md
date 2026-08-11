# DEFAULT-DENY PRIVACY REGISTRY — the framework, and what it does and does not say

The account-deletion cascade universe is 70 tables. Four of them rest on
evidence somebody actually read. The other 66 do not, and pretending otherwise
is how the superseded **73 tables / 31 protected / 6-edge cut-set** figures came
to be quoted in analysis: they were produced by a defective walker and then
elaborated by inference from schema names.

This registry exists so that progress does not require 70 manual classifications
up front, and so that the absence of a classification is *load-bearing* rather
than silently permissive.

    backend/tests/privacy/account_delete_registry.py     the registry
    backend/tests/privacy/cascade_walker.py              the walker + its known-bad twin
    backend/tests/privacy/test_account_delete_registry.py   invariants, anchors, the gate
    backend/tests/privacy/test_cascade_walker_oracle.py     the walker oracle

## `UNCLASSIFIED_BLOCKING` is a workflow state, not a retention classification

It means exactly one thing: **nobody has looked at this table yet.**

It does **not** mean retain indefinitely. It does not mean privacy-approved
retention. It does not mean safe to expose. It does not mean safe to delete.

The registry enforces that reading structurally.
`Entry.protected_from_destructive_cascade` returns `None` — never `False` — for
an unclassified table. `False` is a positive claim ("proven safe to destroy"),
and a caller writing

    if not entry.protected_from_destructive_cascade:
        drop_it()

against a `False` that really meant "unknown" is the precise accident this
design prevents. `test_an_unclassified_entry_never_reads_as_approved_for_deletion`
asserts the distinction by identity, not by truthiness.

`UNCLASSIFIED_BLOCKING` is also kept out of `VALID_CLASSIFICATIONS` entirely, so
it cannot leak into the retention vocabulary by being passed where a
classification is expected.

## Two questions, deliberately separated

**Question A — evidence-survival safety.** *Does deleting the account destroy
something that has to survive?*

Answerable from a single proven-retained table. It does not wait on the other
66. **It is already answered, and the answer is unsafe:**
`ioe.optimization_run` is at depth 1 — a direct `ON DELETE CASCADE` child of
`identity.user_account` — and replay verification reads its sealed hashes to
prove a historical optimization can be reproduced. So
`DELETE FROM identity.user_account` destroys retained evidence *today*. The
root-first rule generalises this: one proven-retained direct cascade child is
sufficient to condemn the root edge, and no descendant classification is needed
to reach that conclusion.

**Question B — terminal privacy completeness.** *Is it safe to run the terminal
account delete?*

Answerable only when every cascade-reachable table carries a classification.
`assert_terminal_account_delete_ready()` is the gate and it fails closed, naming
the count and the state, on all 66.

A failing B does not block A. An answered A does not discharge B.

## What is actually classified, and on what evidence

| table | classification | evidence |
|---|---|---|
| `ioe.optimization_run` | `REPLAY_REQUIRED_RETAIN` | replay verification reads the sealed result hash; the purge test asserts the run count is unchanged |
| `ioe.run_rule_snapshot` | `SEALED_IMMUTABLE_RETAIN` | the purge test captures the rule pin and asserts it is byte-identical afterwards |
| `reco.recommendation` | `LIVE_USER_DATA_DELETE` | owner-scoped `GET /recommendations` reads it straight back to the subject |
| `ioe.freshness_outbox` | `DERIVED_DELETE` | schema comment: transactional outbox, drained and deleted in ordinary operation |

Each entry cites `path:line`, and `test_every_cited_evidence_reference_resolves`
checks the file exists and the line number is real — a citation that rots is
caught rather than becoming folklore.

### One recorded conflict

`app/privacy/classification.py` declares `ioe.run_rule_snapshot` as
`CASCADE_DELETE`. `tests/security/test_sealed_history_after_purge.py` captures
its rule pin and asserts the bytes are identical after the account purge. The
test measures behaviour; the declaration states intent. **The registry follows
the measurement**, and the disagreement is written down in the entry rather than
smoothed over. This is also why the existing classification registry was not
bulk-imported as a seed: it is a declaration, and a declaration is not evidence.

## The walker got its own oracle, because it was wrong once

The certified universe is the output of a recursive query over `pg_constraint`.
An earlier version recursed when the edge it had **already traversed** was
`ON DELETE CASCADE`, instead of when the edge it was **about to** traverse was.
A `SET NULL` child of a cascade-reachable table was therefore reported as
cascade-reachable — and `SET NULL` is exactly the severance mechanism, so the
defect systematically over-reported destruction.

`cascade_walker.py` keeps that defect on purpose as
`KNOWN_BAD_CASCADE_WALK_SQL`. It is never used for analysis. It exists so the
oracle can prove it discriminates: a synthetic schema on which both walkers
agreed would be a synthetic schema that tests nothing.

The synthetic schema (created and dropped inside a rolled-back transaction)
contains CASCADE→CASCADE, CASCADE→SET NULL, CASCADE→NO ACTION, CASCADE beneath a
severed edge, a transitive depth-3 chain, and a parallel path where the short
route must win. The correct walker answers it exactly; the defective one adds
precisely the two children reached across a non-CASCADE edge.

The registry's membership and depths are then re-derived from the **live**
schema on every run, so a migration that adds a cascading FK to a new table
fails a test instead of silently widening what an account delete destroys.

### A measurement note about stale databases

While building this, an ad-hoc run against a leftover `onyx_test` database
reported **72** tables and **40** direct inbound edges, including
`identity.account_lifecycle` and `audit.data_deletion_request` as direct CASCADE
children. Rebuilding the database from `backend/db/sql` reproduced the certified
**70 / 38 (34 CASCADE, 4 SET NULL)** exactly, with neither of those two present:
`44_pd9_durable_deletion_ledger.sql` drops both constraints so the deletion
ledger outlives the account it describes (PD-9). The 72 was a stale database, not
a schema defect — but it is worth recording that the difference between the two
is invisible without applying the schema from scratch.

## Scope, explicitly

Not done here, and not implied by anything above: migration `0060` was not
written, no cascade edge was re-pointed, and no lifecycle phase was wired. The
registry is an analysis and gating artifact. Classifying the remaining 66 is the
work it exists to make safe and incremental.
