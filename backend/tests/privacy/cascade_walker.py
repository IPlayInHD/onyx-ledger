"""Cascade-reachability walker over `pg_constraint`, and the walker that was wrong.

WHY THIS IS A MODULE AND NOT A SHELL SNIPPET. The 70-table certified universe in
`docs/privacy/11b6-account-delete-cascade-universe.md` is the input to every
retention classification. It came out of a recursive query. An earlier version of
that query was defective and produced 73 tables, 31 "protected", and a phantom
sixth cut edge — figures that were quoted in analysis before anyone noticed. A
query that decides what survives an account deletion is production-grade logic
and gets an oracle, the same as any other.

THE DEFECT. The walker recursed when the edge it had ALREADY TRAVERSED was
`ON DELETE CASCADE`, instead of when the edge it was ABOUT TO traverse was. So a
`SET NULL` child of a cascade-reachable table was reported as cascade-reachable.
`SET NULL` is exactly the mechanism that severs a subject from retained
evidence, so the bug systematically over-reported destruction — it claimed rows
would be deleted that in fact survive with a nulled owner.

`KNOWN_BAD_CASCADE_WALK_SQL` preserves that defect on purpose. It is never used
for analysis. It exists so `test_cascade_walker_oracle.py` can prove the oracle
discriminates: a synthetic schema on which both walkers agreed would be a
synthetic schema that tests nothing.
"""

from __future__ import annotations

# Foreign keys as directed edges. `child` references `parent`; `act` is
# `pg_constraint.confdeltype` — 'c' CASCADE, 'n' SET NULL, 'a' NO ACTION,
# 'r' RESTRICT, 'd' SET DEFAULT.
_FK_CTE = """
    fk AS (
        SELECT cn.nspname || '.' || cc.relname AS child,
               pn.nspname || '.' || pc.relname AS parent,
               con.confdeltype                 AS act
          FROM pg_constraint con
          JOIN pg_class     cc ON cc.oid = con.conrelid
          JOIN pg_namespace cn ON cn.oid = cc.relnamespace
          JOIN pg_class     pc ON pc.oid = con.confrelid
          JOIN pg_namespace pn ON pn.oid = pc.relnamespace
         WHERE con.contype = 'f'
    )
"""

#: Correct: recursion is gated on the OUTBOUND edge `f.act`.
CASCADE_WALK_SQL = f"""
WITH RECURSIVE {_FK_CTE},
    walk AS (
        SELECT f.child, 1 AS d
          FROM fk f
         WHERE f.parent = %(root)s AND f.act = 'c'
        UNION
        SELECT f.child, w.d + 1
          FROM fk f
          JOIN walk w ON f.parent = w.child
         WHERE f.act = 'c' AND w.d < %(max_depth)s
    )
SELECT child, min(d) AS depth
  FROM walk
 GROUP BY child
 ORDER BY min(d), child
"""

#: Defective on purpose: recursion is gated on the INBOUND edge `w.act`.
#: Regression fixture only. Never use this to classify anything.
KNOWN_BAD_CASCADE_WALK_SQL = f"""
WITH RECURSIVE {_FK_CTE},
    walk AS (
        SELECT f.child, f.act, 1 AS d
          FROM fk f
         WHERE f.parent = %(root)s AND f.act = 'c'
        UNION
        SELECT f.child, f.act, w.d + 1
          FROM fk f
          JOIN walk w ON f.parent = w.child
         WHERE w.act = 'c' AND w.d < %(max_depth)s
    )
SELECT child, min(d) AS depth
  FROM walk
 GROUP BY child
 ORDER BY min(d), child
"""


def cascade_reachable(cur, root: str, max_depth: int = 12) -> dict[str, int]:
    """Tables reachable from `root` through an unbroken chain of CASCADE edges.

    Returns `{'schema.table': shortest_depth}`. `root` is `'schema.table'`.
    """
    cur.execute(CASCADE_WALK_SQL, {"root": root, "max_depth": max_depth})
    return {row[0]: row[1] for row in cur.fetchall()}


def cascade_reachable_known_bad(cur, root: str, max_depth: int = 12) -> dict[str, int]:
    """The defective walker. Regression fixture only — see the module docstring."""
    cur.execute(KNOWN_BAD_CASCADE_WALK_SQL, {"root": root, "max_depth": max_depth})
    return {row[0]: row[1] for row in cur.fetchall()}


def direct_inbound_edges(cur, root: str) -> list[tuple[str, str]]:
    """Every direct FK into `root`, as `(child, confdeltype)`, one row per edge.

    Includes non-CASCADE edges: a `SET NULL` inbound edge is the severance
    mechanism, and the root-first analysis needs to see it, not just the
    cascades. Returned as edges rather than a `{child: act}` mapping because one
    table can reference the root twice with different actions, and collapsing
    that would hide exactly the case worth seeing.
    """
    cur.execute(
        f"WITH RECURSIVE {_FK_CTE} SELECT child, act FROM fk WHERE parent = %(root)s"
        " ORDER BY child, act",
        {"root": root},
    )
    return [(row[0], row[1]) for row in cur.fetchall()]
