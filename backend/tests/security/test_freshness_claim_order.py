"""Entry 11B5H2D — the claim hands back a batch in the order it selected it.

`ioe.claim_freshness_events` picks WHICH rows to claim with
`ORDER BY c.created_at, c.id`, and the relay then applies the returned rows in
whatever order they arrive in. Those two orders have to be the same order. They
were not: the function returned `ORDER BY c.id`, and `ref.uuid_generate_v7`
builds an id from a 48-bit MILLISECOND timestamp plus `gen_random_bytes` — no
monotonic counter — so two events created within the same millisecond sorted at
random.

Measured before the fix, 200 trials queueing two events back to back in
separate transactions: 82 pairs landed in the same millisecond and 48 came back
reversed. After: 0.

This is not a cosmetic ordering preference. Freshness reasons are
first-cause-wins by predicate — only rows still `current` are updated — so the
order the relay applies a batch in decides which reason a person is shown, and
"your figures moved" and "a rule changed" tell them to do different things.

The test is statistical because the defect was. A single pair reproduces it
only about half the time, so one ordered pair would pass on a broken function
every other run — the kind of test that is worse than none. Many pairs in one
batch make an accidental pass vanishingly unlikely: a scrambled batch of 12
matches queue order with probability 1/12!.
"""
from __future__ import annotations

import uuid

import psycopg2

from tests.conftest import owner_dsn

PAIRS = 12

#: Rounds of 200 to claim before giving up looking for this test's own events.
#: A runaway guard, not a queue-size assumption: the real exits are "all of
#: this test's events have been handed out" and "the queue is empty". 200
#: rounds is 40,000 events, far past anything a suite rerun builds, and the
#: assertion says so if it is ever reached.
_MAX_CLAIM_ROUNDS = 200


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def test_a_claimed_batch_comes_back_in_queue_order():
    """Queue 12 events in separate transactions, claim them in one batch, and
    require the returned order to equal the queued order exactly."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = uuid.uuid4()
        cur.execute("INSERT INTO identity.user_account (id, email, status) "
                    "VALUES (%s, %s, 'active')",
                    (str(user), f"ord_{uuid.uuid4().hex[:10]}@example.com"))

        # Separate statements under autocommit, so each row gets its own
        # transaction timestamp — which is what two real user actions produce.
        queued: list[str] = []
        created: list[object] = []
        for _ in range(PAIRS):
            cur.execute("""
                INSERT INTO ioe.freshness_outbox
                    (event_type, stale_reason_code, user_id, dedupe_key)
                VALUES ('financial_data_changed', 'BASELINE_INPUTS_CHANGED',
                        %s, %s)
                RETURNING id, created_at
            """, (str(user), f"ord:{uuid.uuid4()}"))
            event_id, created_at = cur.fetchone()
            queued.append(str(event_id))
            created.append(created_at)

        # Guard on the guard #1: the events must actually be close enough
        # together to exercise the defect. Spread across many milliseconds, id
        # order and creation order agree and the old function passed too.
        span_ms = (created[-1] - created[0]).total_seconds() * 1000
        assert span_ms < 50, (
            f"the fixture spread {PAIRS} events over {span_ms:.1f}ms; they are "
            "too far apart to exercise same-millisecond ordering")

        # Guard on the guard #2: creation timestamps must be distinct, or there
        # is no queue order to preserve and the assertion below is meaningless.
        assert len(set(created)) == PAIRS, (
            "events share a created_at; separate transactions were expected")

        # The whole queue is shared with every other test in the run, and older
        # pending events legitimately sort ahead of these. So claim the maximum
        # and compare only THIS test's events, as a subsequence.
        #
        # An earlier version asserted `returned == queued` against a batch of
        # exactly PAIRS, which passed alone and failed in the full suite —
        # measuring queue emptiness rather than queue order.
        # ONE CALL IS NOT ENOUGH, and asking for a bigger batch cannot help:
        # `claim_freshness_events` clamps its batch to 200
        # (`least(greatest(coalesce(p_batch_size,1),1), 200)`). These events are
        # the newest in the queue, so they sort LAST — and Entry 11B5J measured
        # this test failing with "claimed 0 of this test's 12 events" against a
        # backlog of 452 claimable rows, while production ordering was correct.
        #
        # Claiming in rounds and concatenating preserves the property under
        # test: every round takes the oldest remaining under
        # `ORDER BY created_at, id`, so the concatenation is in the same global
        # order a relay would see across successive claims.
        wanted = set(queued)
        returned: list[str] = []
        for _ in range(_MAX_CLAIM_ROUNDS):
            cur.execute(
                "SELECT out_event_id FROM ioe.claim_freshness_events(200, %s)",
                ("order-test",))
            batch = [str(r[0]) for r in cur.fetchall()]
            if not batch:
                break
            returned.extend(batch)
            if wanted.issubset(returned):
                break
        mine = [e for e in returned if e in wanted]

        assert len(mine) == PAIRS, (
            f"claimed {len(mine)} of this test's {PAIRS} events across "
            f"{_MAX_CLAIM_ROUNDS} rounds of 200; their relative order was not "
            "measured")
        assert mine == queued, (
            "the claim returned a batch in a different order than it selected "
            "it, so the relay applies events out of queue order:\n"
            f"  queued:   {queued}\n  returned: {mine}")
    finally:
        admin.close()


def test_the_returned_order_is_stated_in_the_function_itself():
    """Read the installed definition, so a later edit cannot quietly restore
    the id-only sort while the statistical test above happens to pass."""
    admin = _owner()
    try:
        cur = admin.cursor()
        cur.execute("SELECT prosrc FROM pg_proc p JOIN pg_namespace n "
                    "  ON n.oid = p.pronamespace "
                    " WHERE n.nspname = 'ioe' "
                    "   AND p.proname = 'claim_freshness_events'")
        body = cur.fetchone()[0]
    finally:
        admin.close()

    # The final ORDER BY is the one that decides processing order.
    tail = body[body.rindex("FROM claimed c"):]
    assert "ORDER BY c.created_at, c.id" in tail, (
        "the claim no longer returns its batch in queue order; ids are "
        "millisecond-precision plus randomness, so an id-only sort scrambles "
        f"same-millisecond events:\n{tail}")
