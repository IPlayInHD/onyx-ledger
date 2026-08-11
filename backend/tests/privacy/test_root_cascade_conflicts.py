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


def test_every_proven_retained_table_is_reported_with_its_exposing_root(cur):
    """The conflict inventory. Not an assertion that it is empty — it is not.

    This is Question A at full width: for each table proven to need survival,
    which direct child of the account exposes it to the cascade. Those roots are
    where a cut would have to go; choosing the mechanism is not this slice's
    job.
    """
    conflicts = _conflicts(cur)
    assert conflicts, (
        "no proven-retained table is cascade-reachable. Either the retention "
        "proofs or the graph changed materially — re-establish Question A "
        "before relying on it."
    )
    for table, roots in sorted(conflicts.items()):
        assert roots, f"{table} is reachable but no direct root explains it"


def test_the_conflict_set_covers_every_proven_retained_table(cur):
    """A retained table that is NOT reachable would be genuinely safe today.

    Splitting the two is the point: it stops "proven retained" from being read
    as "currently in danger", and would show up immediately if a cut landed.
    """
    reachable = cascade_reachable(cur, ROOT)
    retained = set(proven_retained_tables())
    safe = sorted(retained - set(reachable))
    assert safe == [], (
        f"these proven-retained tables are no longer cascade-reachable: {safe}. "
        "That is good news and must be recorded deliberately, not absorbed."
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
    assert len(conflicts) + len(blocking) < len(REGISTRY), (
        "every table is either a known conflict or unmeasured, which would mean "
        "nothing has been proven deletable — check the partition helpers"
    )


def test_the_three_root_branches_are_direct_cascade_children(cur):
    """The roots this slice classified really are depth-1 cascade children.

    Their classifications rest on that: a retained table hanging off a direct
    `ON DELETE CASCADE` from the account is what makes the root edge itself the
    thing that must change.
    """
    edges = {child: act for child, act in direct_inbound_edges(cur, ROOT)}
    for table in ("analysis.analysis_run", "ioe.scenario", "ioe.integrity_check"):
        assert edges.get(table) == "c", (
            f"{table} is no longer a direct ON DELETE CASCADE child of {ROOT} "
            f"(edge={edges.get(table)!r}); its classification rationale cites "
            "that fact and must be rebuilt"
        )
        assert REGISTRY[table].depth == 1
