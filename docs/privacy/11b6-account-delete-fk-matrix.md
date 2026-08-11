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
