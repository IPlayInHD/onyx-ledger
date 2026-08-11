# Entry 11B6C — account-delete cascade audit

**Verdict: UNSAFE.** Production cannot `DELETE FROM identity.user_account`
today. A plain delete destroys sealed evidence that Entry 11B5H3 certified
must survive account deletion.

## The graph, from catalog evidence

    direct FKs to identity.user_account     38   (34 CASCADE, 4 SET NULL)
    transitive reachable tables             73
    maximum cascade depth                    5

The previous slice's probe deleted an account with **no dependent rows**. It
proved only that the PD-9 ledger survives. Depth 1 is not the graph.

## The blocker: sealed evidence is cascade-reachable

31 tables reachable by CASCADE-only paths carry historical/replay evidence:

| Depth | Table | Why it must survive |
|---|---|---|
| 1 | `ioe.integrity_check` | integrity verification history |
| 1 | `ioe.optimization_run`, `ioe.scenario` | sealed optimization/scenario evidence |
| 1 | `analysis.analysis_run` | analysis history |
| 2 | `analysis.analysis_input_snapshot` | **frozen input snapshot** |
| 2 | `ioe.run_rule_snapshot`, `ioe.run_rule_version` | **rule version pins** |
| 2 | `ioe.scenario_result`, `ioe.multi_year_projection` | sealed results |
| 3 | `ioe.resource_ledger_entry` | ledger |

…plus 23 further `ioe.*`/`analysis.*` children at depths 2–3.

Entry 11B5H3 certified sealed-history preservation and replay after live-source
purge. A cascading account delete would remove exactly the artifacts those
proofs depend on. Classified **DESIGN_DEFECT**, and it blocks MODEL A until
resolved.

## Classification (initial)

    RETAIN_IMMUTABLE_EVIDENCE      31  cascade-reachable — MUST CHANGE
    RETAIN_DURABLE_LEDGER           1  identity.account_lifecycle (no FK; safe)
    DEIDENTIFY_THEN_RETAIN          –  audit/auth, per 0056–0059
    SET_NULL_AND_RETAIN             4  the four ON DELETE SET NULL edges
    UNRESOLVED_REQUIRES_DECISION   37  remaining reachable tables

Not every reachable table is classified yet; the 31 above are sufficient to
stop MODEL A, so the rest is the next slice's work.

## Required before any terminal delete phase

1. Sever or re-point the CASCADE edges that reach sealed evidence — likely
   `ON DELETE SET NULL` plus a pseudonymous subject key, but each needs proof
   the surviving row stays coherent and RLS-protected without `user_id`.
2. Decide RLS for retained rows whose owning account is gone.
3. Object-storage deletion is not covered by any FK and remains separate.

## Note on audit

`audit.audit_log` does **not** appear in the cascade set — it has no FK to
`user_account` by design, so audit history survives account deletion already.
`audit.data_export_request` does appear (depth 1) and needs classification.

## Status

    Can production safely DELETE identity.user_account today?   NO
    Blocker                                                     31 evidence
                                                                tables
    PD-9                                                        CLOSED, ledger
                                                                survives

---

# Minimum cascade cut-set (computed, Entry 11B6C)

**31 protected tables, but only 6 root edges need changing.** Cutting
descendant FKs is unnecessary once the branch root survives.

| # | Cut edge (from `identity.user_account`) | Protected descendants saved | Branch depth |
|---|---|---|---|
| 1 | `analysis.analysis_run` | 32 | 3 |
| 2 | `ioe.optimization_run` | 20 | 2 |
| 3 | `ioe.scenario` | 9 | 1 |
| 4 | `ioe.integrity_check` | 1 (itself) | 0 |
| 5 | `ioe.freshness_outbox` | 1 (itself) | 0 |
| 6 | **`reco.recommendation`** | 1 — `ioe.optimization_candidate` | 2 |

Branch counts overlap because several protected tables are reachable by more
than one path; the union is the 31 protected tables.

## Why edge 6 matters

Cutting only the five obvious roots leaves **one** protected table still
reachable:

    protected reachable BEFORE cut : 78 paths / 70 tables
    after cutting 5 roots          : 1 protected still reachable
    the survivor                   : ioe.optimization_candidate
                                     via reco.recommendation, depth 2

`reco.recommendation` is not itself evidence and would not be selected by any
name-based or depth-1 heuristic — but it carries a CASCADE into sealed
optimization candidates. A cut-set chosen by inspection rather than by
computing reachability would have destroyed them.

## Status

    protected tables      31
    root edges to change   6
    verification           recompute reachability after the migration and
                           require intersection(CASCADE_REACHABLE, PROTECTED)
                           to be empty

---

# Corrections from the semantic analysis (Entry 11B6C continuation)

Two of the six "cut edges" were wrong. Both errors were mine.

## 1. The sixth edge does not exist — traversal bug

`reco.recommendation` was added because `ioe.optimization_candidate` appeared
reachable through it. It is not:

    ioe.optimization_candidate.recommendation_id
      -> reco.recommendation        ON DELETE SET NULL   (not CASCADE)

A SET NULL edge does not delete the child. `optimization_candidate` survives
that path with its `recommendation_id` nulled, and it is already protected by
cutting `ioe.optimization_run`, which its `run_id` FK cascades from.

The recursive query that produced the finding gated recursion on the action of
the edge that REACHED a node, then enumerated every outbound FK from it
regardless of that edge's own action — so SET NULL children were reported as
cascade-reachable. **The walker over-reported.** Any conclusion drawn from it
about which tables are cascade-reachable is suspect until re-run with the
action tested on the outbound edge.

## 2. `ioe.freshness_outbox` is not evidence

Its own comment: *"Transactional outbox for freshness invalidation. Written in
the same transaction as the change that caused it, so an event exists if and
only if the change committed."* Delivery infrastructure, with no retention or
purge semantics anywhere in the schema. Preserving processed queue residue past
account deletion has no justification.

Reclassified `DERIVED_DELETE`; removed from the protected set.

## Revised position

    previously reported cut-set     6 edges
    reco.recommendation             REMOVE — traversal artifact
    ioe.freshness_outbox            REMOVE — not evidence
    remaining candidates            analysis.analysis_run
                                    ioe.optimization_run
                                    ioe.scenario
                                    ioe.integrity_check

Four candidates, NOT yet confirmed: the corrected traversal has not been re-run,
so the protected set itself (previously 31) must be recomputed before any
migration is planned. `ioe.integrity_check` also still needs the §9 justification
for being in the protected set at all.

---

# Certified cascade graph (corrected walker)

## The walker defect

    WRONG   recurse when the edge that REACHED this node was CASCADE
            ... WHERE w.act='c'      -- w = the inbound edge
    RIGHT   recurse only when THIS outbound edge is CASCADE
            ... WHERE f.act='c'      -- f = the edge being traversed

The wrong form admitted SET NULL children into the deletion closure, because a
child reached by a SET NULL edge is not deleted at all.

## Certified numbers

| | previous claim | corrected |
|---|---|---|
| direct inbound FKs | 38 / 34 CASCADE / 4 SET NULL | **confirmed, remeasured** |
| cascade-reachable tables | 73 | **70** |
| `ioe.`/`analysis.` reachable | 31 | **30** |

Cross-checked by two independent implementations sharing no traversal code — a
recursive SQL CTE over `pg_constraint` and a Python BFS over a flat 248-edge
inventory. Both report **70**. Depth differs by construction (CTE 4 = longest
path, BFS 3 = shortest) and is a reporting difference, not a disagreement on the
set.

## The recommendation branch, settled

`ioe.optimization_candidate` **is** cascade-reachable at depth 2 — but through

    ioe.optimization_candidate.run_id -> ioe.optimization_run   CASCADE

not through `reco.recommendation`, whose edge is SET NULL. So the previous
slice's removal of `reco.recommendation` from the cut-set was correct, though
for a sharper reason than stated then: the table it was supposed to protect is
already covered by the `ioe.optimization_run` cut.

`reco.recommendation` is itself cascade-reachable at depth 1 via its own FK from
`user_account`; that is live recommendation data and is expected to delete.

## Status

    walker defect            fixed
    closure                  recomputed from scratch, independently agreed
    previous 73/31/6 figures SUPERSEDED
    protected set            not yet rebuilt from semantics (30 is a
                             schema-prefix proxy, not a justified set)
