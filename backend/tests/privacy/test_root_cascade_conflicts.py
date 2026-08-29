"""What the account-delete cascade would do to evidence, measured rather than argued.

Two things are established here.

**The schema staleness guard.** Every structural conclusion in this directory is
read out of `pg_catalog`, so it is only as current as the database it ran
against. A leftover database once reported 72 cascade-reachable tables and 40
direct edges where a rebuilt one reports 70 and 38 — the difference being two
constraints that `44_pd9_durable_deletion_ledger.sql` drops. Nothing about that
was visible without applying the schema from scratch. So before any conclusion
is drawn, the database is checked for the shape only the newest migrations
produce.

**The root conflicts.** A conflict is a table proven to require survival that is
nonetheless reachable from `identity.user_account` through an unbroken chain of
`ON DELETE CASCADE`. Each is reported with the minimal direct-account root that
exposes it, because that root is where a future cut has to happen.
"""

from __future__ import annotations

import psycopg2
import pytest

from tests.conftest import owner_dsn
from tests.privacy.account_delete_registry import REGISTRY, proven_retained_tables
from tests.privacy.cascade_walker import cascade_reachable, direct_inbound_edges

ROOT = "identity.user_account"


@pytest.fixture(scope="module")
def cur():
    conn = psycopg2.connect(owner_dsn())
    try:
        yield conn.cursor()
    finally:
        conn.close()


# --------------------------------------------------------- staleness guard ---

def test_the_database_under_test_carries_the_newest_privacy_schema(cur):
    """Fail loudly on a stale database instead of concluding from one.

    Deliberately not a database name and not an `alembic_version` read: the
    suite's own database is built by `scripts/apply_schema.sh`, which never
    stamps a revision, so a version check would pass vacuously there and a name
    check would only assert which database somebody happened to point at. What
    is checked instead is the SHAPE the newest migrations produce.
    """
    cur.execute("SELECT current_database()")
    database = cur.fetchone()[0]

    # 0059 (53_subject_severance.sql) rebuilt this table keyed by the subject,
    # dropping the account id. The 0056 shape had `user_id` as the primary key,
    # so the absence of that column is what distinguishes them.
    cur.execute("""
        SELECT a.attname
          FROM pg_attribute a
         WHERE a.attrelid = 'identity.deletion_subject'::regclass
           AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
    """)
    columns = [row[0] for row in cur.fetchall()]
    assert columns == ["subject_key", "retired_at"], (
        f"database {database!r} has identity.deletion_subject{columns}, which "
        "is not the 0059_subject_severance shape. It is behind the migration "
        "chain, and every structural conclusion drawn from it would be wrong."
    )

    # 44_pd9_durable_deletion_ledger.sql drops both of these so the deletion
    # ledger outlives the account. Their presence is the exact signature of the
    # stale database that reported 72 tables and 40 direct edges.
    cur.execute("""
        SELECT cn.nspname || '.' || cc.relname
          FROM pg_constraint con
          JOIN pg_class cc ON cc.oid = con.conrelid
          JOIN pg_namespace cn ON cn.oid = cc.relnamespace
         WHERE con.contype = 'f'
           AND con.confrelid = 'identity.user_account'::regclass
           AND cn.nspname || '.' || cc.relname
               IN ('identity.account_lifecycle', 'audit.data_deletion_request')
    """)
    undropped = sorted(row[0] for row in cur.fetchall())
    assert undropped == [], (
        f"database {database!r} still carries PD-9 foreign keys that migration "
        f"44 drops: {undropped}. It predates the deletion ledger being made "
        "durable."
    )


# ------------------------------------------------------------- conflicts -----

def _conflicts(cur) -> dict[str, list[str]]:
    """{proven-retained table: minimal direct-account roots that reach it}."""
    reachable = cascade_reachable(cur, ROOT)
    roots = sorted({child for child, act in direct_inbound_edges(cur, ROOT) if act == "c"})
    out = {}
    for table in proven_retained_tables():
        if table not in reachable:
            continue
        exposing = sorted(
            root for root in roots
            if root == table or table in cascade_reachable(cur, root)
        )
        out[table] = exposing
    return out


def test_no_proven_retained_table_is_cascade_reachable_any_more(cur):
    """The conflict inventory, now empty — and that is 0060's whole result.

    Before the migration this reported five conflicts over four direct roots.
    Dropping those four foreign keys removed every all-CASCADE path from the
    account to a table proven to require survival. Asserted as emptiness rather
    than deleted, because a future migration that re-exposes one has to fail
    somewhere.
    """
    conflicts = _conflicts(cur)
    assert conflicts == {}, (
        "proven-retained evidence is cascade-reachable from the account again:\n  "
        + "\n  ".join(
            f"{table} via {', '.join(roots)}" for table, roots in sorted(conflicts.items())
        )
    )


def test_the_four_detached_roots_are_no_longer_direct_cascade_children(cur):
    """The specific edges 0060 removed, named so a regression is unambiguous."""
    edges = {child: act for child, act in direct_inbound_edges(cur, ROOT)}
    for table in ("analysis.analysis_run", "ioe.optimization_run",
                  "ioe.scenario", "ioe.integrity_check"):
        assert table not in edges, (
            f"{table} has a direct foreign key to {ROOT} again "
            f"(action={edges[table]!r}); migration 0060 removed it"
        )


def test_the_known_conflicts_are_distinct_from_unknown_risk(cur):
    """§22 — two different numbers that must never be summed or conflated.

    A blocking table is not proven retained, and is not proven safe either. It
    is unmeasured. Reporting them as one figure would either overstate the
    proven danger or hide it.
    """
    conflicts = _conflicts(cur)
    blocking = [t for t, e in REGISTRY.items() if e.protected_from_destructive_cascade is None]
    assert set(conflicts).isdisjoint(blocking)
    assert len(blocking) == 4, (
        f"{len(blocking)} unmeasured privacy surfaces, expected 27. This number "
        "moves only when a table is genuinely classified from evidence — never "
        "because a foreign key was dropped, which is not a privacy decision."
    )


def test_the_registry_did_not_shrink_when_the_graph_did(cur):
    """§44 — the number that must not move.

    The closure lost 29 tables when four edges went. Every one of them keeps its
    registry entry and, where unclassified, keeps blocking. If this ever fails
    alongside a smaller blocker count, privacy completeness was bought with
    `DROP CONSTRAINT`.

    THE NUMBER MAY GROW, and has: 73 to 74 when B4 added
    identity.legal_acceptance, and 74 to 79 when the BillShield database
    foundation added its five tenant-derived tables. Growth is a migration widening what deletion
    destroys, and the oracle beside this file demands a registry entry before a
    table may enter the closure at all. What THIS assertion forbids is the
    opposite — a table quietly leaving because somebody dropped a foreign key.
    """
    live = set(cascade_reachable(cur, ROOT))
    assert len(REGISTRY) == 79, (
        f"the certified privacy universe is {len(REGISTRY)} tables, not 79"
    )
    assert len(live) < len(REGISTRY), (
        "the live closure is no smaller than the certified universe; 0060's "
        "detachment is not in effect"
    )
