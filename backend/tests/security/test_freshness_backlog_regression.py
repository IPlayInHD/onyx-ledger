"""Entry 11B5J — the freshness tests must survive an unrelated backlog.

Three tests failed together during certification, in one run out of fifteen,
against a database the suite had been run against repeatedly:

    test_a_claimed_batch_comes_back_in_queue_order
        claimed 0 of this test's 12 events
    test_inputs_then_rules_records_the_input_change
        both causes must be consumed, got ['pending', 'pending']
    test_purging_one_tenant_leaves_the_other_tenants_queue_and_data
        the bystander's own event did not take effect

One root cause, and not a production one. Each test assumed a bounded claim or
a bounded drain would reach the events it had just created. It will not:
`claim_freshness_events` clamps its batch to 200 and `FreshnessRelay.drain()`
stops after ten passes, both deliberately, and a test's own events are the
NEWEST rows in a queue ordered `created_at, id` — so they are the last to be
reached, not the first.

Measured threshold before the fixes: 1812 claimable events, all three pass;
3252, all three fail.

That is the kind of failure that gets "fixed" by deleting the queue, which
would remove the very condition the certification gate exists to create. So
this file does the opposite: it MAKES the backlog, on purpose, and requires the
three tests to be right in spite of it.

It removes only the rows it inserted. Nothing here truncates the outbox,
completes another test's events, or assumes an empty queue.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

#: Comfortably past the 3252 that broke all three before the fix, and past the
#: 2000 a single `drain()` can clear, so the bounded-effort assumption cannot
#: come back unnoticed.
_BACKLOG = 3500


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _claimable(cur) -> int:
    cur.execute("SELECT count(*) FROM ioe.freshness_outbox "
                " WHERE claim_state = 'pending' "
                "    OR (claim_state = 'claimed' "
                "        AND claimed_at < now() - ioe.freshness_claim_timeout())")
    return cur.fetchone()[0]


@pytest.fixture
def seeded_backlog():
    """Insert unrelated, legitimate pending events, and remove exactly those.

    Year-scoped `baseline_inputs_changed` events dated in the past: the same
    shape the suite leaves behind, and old enough to sort ahead of anything a
    test creates afterwards — which is the whole point.
    """
    tag = f"backlog:{uuid.uuid4()}"
    admin = _owner()
    cur = admin.cursor()
    cur.execute("""
        INSERT INTO ioe.freshness_outbox
            (event_type, stale_reason_code, tax_year, dedupe_key, created_at)
        SELECT 'baseline_inputs_changed', 'BASELINE_INPUTS_CHANGED', 2025,
               %s || ':' || gen_random_uuid(), now() - interval '1 day'
          FROM generate_series(1, %s)
    """, (tag, _BACKLOG))
    before = _claimable(cur)
    assert before >= _BACKLOG, (
        f"the fixture did not build a backlog: {before} claimable")
    try:
        yield before
    finally:
        # ONLY this fixture's rows. Deleting anything else would erase the
        # accumulated state the certification gate deliberately creates.
        cur.execute("DELETE FROM ioe.freshness_outbox WHERE dedupe_key LIKE %s",
                    (f"{tag}:%",))
        admin.close()


def test_the_claim_order_proof_survives_a_backlog(seeded_backlog):
    """Its events are the newest, so a single capped claim never sees them."""
    from tests.security.test_freshness_claim_order import (
        test_a_claimed_batch_comes_back_in_queue_order as proof,
    )

    proof()


@pytest.mark.asyncio
async def test_the_first_cause_proof_survives_a_backlog(seeded_backlog):
    """A single `drain()` clears 2000 events; the backlog here is larger."""
    from tests.security.test_freshness_multiple_causes import (
        test_inputs_then_rules_records_the_input_change as proof,
    )

    await proof()


@pytest.mark.asyncio
async def test_the_tenant_isolation_proof_survives_a_backlog(seeded_backlog):
    """The bystander's event is behind the whole backlog and must still be
    applied before the test judges tenant isolation."""
    from tests.security.test_cross_tenant_purge_and_freshness import (
        test_purging_one_tenant_leaves_the_other_tenants_queue_and_data as proof,
    )

    await proof()


def test_the_backlog_fixture_removes_only_its_own_rows():
    """The fixture is a test asset, so its cleanup is under test too. A
    fixture that took unrelated events with it would quietly destroy the
    adversarial state the rest of this file depends on."""
    admin = _owner()
    try:
        cur = admin.cursor()
        cur.execute("SELECT count(*) FROM ioe.freshness_outbox")
        before = cur.fetchone()[0]

        tag = f"backlog:{uuid.uuid4()}"
        cur.execute("""
            INSERT INTO ioe.freshness_outbox
                (event_type, stale_reason_code, tax_year, dedupe_key, created_at)
            SELECT 'baseline_inputs_changed', 'BASELINE_INPUTS_CHANGED', 2025,
                   %s || ':' || gen_random_uuid(), now() - interval '1 day'
              FROM generate_series(1, 25)
        """, (tag,))
        cur.execute("SELECT count(*) FROM ioe.freshness_outbox")
        assert cur.fetchone()[0] == before + 25, "the seed did not insert"

        cur.execute("DELETE FROM ioe.freshness_outbox WHERE dedupe_key LIKE %s",
                    (f"{tag}:%",))
        assert cur.rowcount == 25, (
            f"the cleanup matched {cur.rowcount} rows, not the 25 it inserted")

        cur.execute("SELECT count(*) FROM ioe.freshness_outbox")
        assert cur.fetchone()[0] == before, (
            "the cleanup did not restore the queue to its previous size")
    finally:
        admin.close()
