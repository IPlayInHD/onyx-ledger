"""An oracle for the cascade-reachability walker, on a schema built to break it.

The 70-table certified universe is only as good as the query that produced it,
and an earlier version of that query was wrong in a way nobody caught by reading
it. So the walker is tested the way any other decision procedure is: against a
schema whose correct answer is known by construction, containing precisely the
shapes that distinguish a right walker from the wrong one.

The synthetic schema is created and dropped inside a rolled-back transaction, so
it never exists for any other session and leaves nothing behind.
"""

from __future__ import annotations

import psycopg2
import pytest

from tests.conftest import owner_dsn
from tests.privacy.account_delete_registry import REGISTRY
from tests.privacy.cascade_walker import (
    cascade_reachable,
    cascade_reachable_known_bad,
    direct_inbound_edges,
)

ROOT = "identity.user_account"

# Every edge shape that matters, and one that only a walker with the inbound-edge
# bug gets wrong (`g_setnull`).
SYNTHETIC_DDL = """
CREATE SCHEMA walkoracle;

CREATE TABLE walkoracle.root       (id int PRIMARY KEY);
CREATE TABLE walkoracle.c_casc     (id int PRIMARY KEY,
    root_id int REFERENCES walkoracle.root(id) ON DELETE CASCADE);
CREATE TABLE walkoracle.c_setnull  (id int PRIMARY KEY,
    root_id int REFERENCES walkoracle.root(id) ON DELETE SET NULL);
CREATE TABLE walkoracle.c_noaction (id int PRIMARY KEY,
    root_id int REFERENCES walkoracle.root(id) ON DELETE NO ACTION);

-- CASCADE under CASCADE: reachable at depth 2.
CREATE TABLE walkoracle.g_casc     (id int PRIMARY KEY,
    p int REFERENCES walkoracle.c_casc(id) ON DELETE CASCADE);
-- SET NULL under CASCADE: NOT reachable. The row survives with a nulled owner.
CREATE TABLE walkoracle.g_setnull  (id int PRIMARY KEY,
    p int REFERENCES walkoracle.c_casc(id) ON DELETE SET NULL);
-- NO ACTION under CASCADE: NOT reachable. The delete is refused, not propagated.
CREATE TABLE walkoracle.g_noaction (id int PRIMARY KEY,
    p int REFERENCES walkoracle.c_casc(id) ON DELETE NO ACTION);

-- Transitive depth 3.
CREATE TABLE walkoracle.gg_casc    (id int PRIMARY KEY,
    p int REFERENCES walkoracle.g_casc(id) ON DELETE CASCADE);

-- CASCADE beneath a severed edge: unreachable, because the chain is broken above.
CREATE TABLE walkoracle.under_setnull (id int PRIMARY KEY,
    p int REFERENCES walkoracle.c_setnull(id) ON DELETE CASCADE);

-- Two CASCADE routes in, one short and one long. Shortest path must win.
CREATE TABLE walkoracle.parallel   (id int PRIMARY KEY,
    root_id int REFERENCES walkoracle.root(id) ON DELETE CASCADE,
    deep_id int REFERENCES walkoracle.gg_casc(id) ON DELETE CASCADE);
"""

SYNTHETIC_ROOT = "walkoracle.root"

#: Correct answer, by construction.
EXPECTED = {
    "walkoracle.c_casc": 1,
    "walkoracle.parallel": 1,
    "walkoracle.g_casc": 2,
    "walkoracle.gg_casc": 3,
}

#: What the defective walker adds: children reached across a non-CASCADE edge.
KNOWN_BAD_EXTRA = {"walkoracle.g_setnull", "walkoracle.g_noaction"}


@pytest.fixture
def synthetic_cur():
    """A cursor on a rolled-back transaction holding the synthetic schema."""
    conn = psycopg2.connect(owner_dsn())
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(SYNTHETIC_DDL)
        yield cur
    finally:
        conn.rollback()
        conn.close()


def test_the_walker_answers_the_synthetic_schema_exactly(synthetic_cur):
    """CASCADE chains in, SET NULL and NO ACTION out, shortest path for depth."""
    assert cascade_reachable(synthetic_cur, SYNTHETIC_ROOT) == EXPECTED


def test_set_null_under_cascade_is_not_reachable(synthetic_cur):
    """The exact shape the defective walker got wrong, called out on its own.

    `g_setnull` references a cascade-reachable parent, but through `SET NULL`.
    Deleting the root does not delete it — it nulls the pointer. A walker that
    reports it as destroyed will under-count what survives, which is the failure
    mode that matters for evidence retention.
    """
    reachable = cascade_reachable(synthetic_cur, SYNTHETIC_ROOT)
    assert "walkoracle.g_setnull" not in reachable
    assert "walkoracle.g_noaction" not in reachable
    assert "walkoracle.under_setnull" not in reachable


def test_the_parallel_path_reports_the_shortest_depth(synthetic_cur):
    """`parallel` is reachable at 1 directly and at 4 through the deep chain."""
    assert cascade_reachable(synthetic_cur, SYNTHETIC_ROOT)["walkoracle.parallel"] == 1


def test_the_known_bad_walker_still_fails_this_schema(synthetic_cur):
    """The regression guard on the guard.

    If this ever passes, the synthetic schema has stopped containing the shape
    that distinguishes a correct walker from the one that produced the
    superseded 73/31/6 figures — and every test above became decorative.
    """
    bad = cascade_reachable_known_bad(synthetic_cur, SYNTHETIC_ROOT)
    assert bad != EXPECTED, "the synthetic schema no longer discriminates the walkers"
    assert set(bad) - set(EXPECTED) == KNOWN_BAD_EXTRA
    assert not set(EXPECTED) - set(bad), "the defect under-reported; it over-reports"


def test_the_bounded_depth_does_not_truncate_the_synthetic_answer(synthetic_cur):
    """A depth bound below the real depth must lose rows, and 12 must not."""
    assert cascade_reachable(synthetic_cur, SYNTHETIC_ROOT, max_depth=1) == {
        "walkoracle.c_casc": 1,
        "walkoracle.parallel": 1,
    }
    assert cascade_reachable(synthetic_cur, SYNTHETIC_ROOT, max_depth=12) == EXPECTED


@pytest.fixture
def live_cur():
    conn = psycopg2.connect(owner_dsn())
    try:
        yield conn.cursor()
    finally:
        conn.close()


def test_the_registry_membership_is_what_the_live_schema_says_today(live_cur):
    """The certified universe is not a snapshot to be trusted — it is re-derived.

    A migration that adds a cascading FK to a new table silently widens what an
    account delete destroys. Recomputing here is what turns that into a failing
    test instead of a discovery after the fact.
    """
    live = cascade_reachable(live_cur, ROOT)
    missing = sorted(set(live) - set(REGISTRY))
    extra = sorted(set(REGISTRY) - set(live))
    assert missing == [], f"cascade-reachable tables absent from the registry: {missing}"
    assert extra == [], f"registry tables no longer cascade-reachable: {extra}"


def test_the_registry_depths_match_the_live_schema(live_cur):
    live = cascade_reachable(live_cur, ROOT)
    drifted = {
        table: (entry.depth, live[table])
        for table, entry in REGISTRY.items()
        if table in live and entry.depth != live[table]
    }
    assert drifted == {}, f"registry depth disagrees with the live graph: {drifted}"


def test_the_root_has_direct_severance_edges_as_well_as_cascades(live_cur):
    """Root-first: the root's own inbound edges are the first thing to classify.

    Not an equality assertion on a count, which would only re-state the fixture.
    What matters is that both kinds exist — the graph really does contain edges
    that sever rather than destroy, so 'reachable' and 'referencing' are
    different questions at the root itself.
    """
    edges = direct_inbound_edges(live_cur, ROOT)
    actions = {act for _, act in edges}
    assert "c" in actions, "no cascading FK into the root; the universe is vacuous"
    assert "n" in actions, "no severing FK into the root; the walker cannot be exercised"
    cascading = {child for child, act in edges if act == "c"}
    assert cascading <= set(REGISTRY), (
        f"direct cascade children missing from the registry: "
        f"{sorted(cascading - set(REGISTRY))}"
    )
