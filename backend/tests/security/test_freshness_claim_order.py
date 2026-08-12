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

THE TIE IS NOW CONSTRUCTED, NOT RACED. The first version queued 12 events back
to back and asserted they had landed within 50ms of each other, on the theory
that a fast machine would put them in the same millisecond. That made the
FIXTURE depend on runner speed: it was measured failing its own precondition at
72.7ms and 160.5ms on CI, twice, on commits that changed no code. A test that
reports "the fixture was too slow" cannot report anything about the function.

So the ids and creation times are supplied explicitly instead. Every event gets
the SAME 48-bit millisecond field and a `rand_a` that DESCENDS, which makes
byte-wise uuid order the exact reverse of queue order; creation times ascend by
one microsecond inside that same millisecond. The adversarial case is therefore
guaranteed on every run rather than hoped for, and a function that sorted by id
would now fail every time instead of half the time. That is strictly stronger
than the statistical version it replaces — nothing was weakened to remove the
flake, and the production invariant is untouched.
"""
from __future__ import annotations

import uuid

import psycopg2

from tests.conftest import owner_dsn

PAIRS = 12


def _same_millisecond_uuid(prefix_hex: str, ordinal: int) -> str:
    """A v7-shaped uuid sharing `prefix_hex` as its millisecond field.

    Layout: 48-bit ms | version 7 | rand_a | variant | rand_b. The ordinal goes
    in `rand_a`, which is the first field that differs, so ordering between
    these ids is decided entirely by it. `rand_b` stays random so repeated runs
    cannot collide on the primary key.
    """
    body = (
        prefix_hex                  # bytes 0-5: the shared millisecond
        + "7"                       # version
        + f"{ordinal:03x}"          # rand_a: what decides the sort
        + "8"                       # RFC-4122 variant
        + uuid.uuid4().hex[:15]     # rand_b: never reached by the comparison
    )
    return (f"{body[:8]}-{body[8:12]}-{body[12:16]}-{body[16:20]}-{body[20:32]}")

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

        # One real generated id donates a realistic millisecond field; the rest
        # is ours, so the same-millisecond collision is built rather than raced.
        cur.execute("SELECT ref.uuid_generate_v7()")
        prefix_hex = str(cur.fetchone()[0]).replace("-", "")[:12]
        base_ms = int(prefix_hex, 16)

        # rand_a DESCENDS while created_at ASCENDS, so the two sorts disagree on
        # every single pair — the worst case for the defect, not a sample of it.
        queued: list[str] = []
        created: list[object] = []
        for offset in range(PAIRS):
            event_id = _same_millisecond_uuid(prefix_hex, PAIRS - 1 - offset)
            cur.execute("""
                INSERT INTO ioe.freshness_outbox
                    (id, event_type, stale_reason_code, user_id, dedupe_key,
                     created_at)
                VALUES (%s, 'financial_data_changed', 'BASELINE_INPUTS_CHANGED',
                        %s, %s,
                        to_timestamp(0)
                          + (%s::bigint * interval '1 millisecond')
                          + (%s::int * interval '1 microsecond'))
                RETURNING id, created_at
            """, (event_id, str(user), f"ord:{uuid.uuid4()}", base_ms, offset))
            written_id, created_at = cur.fetchone()
            queued.append(str(written_id))
            created.append(created_at)

        # Guard on the guard #1: every event must share one millisecond field.
        # Spread across several milliseconds, id order and creation order agree
        # and the OLD function passed too — that is exactly the hole the timing
        # precondition was trying, unreliably, to cover.
        prefixes = {q.replace("-", "")[:12] for q in queued}
        assert prefixes == {prefix_hex}, (
            f"the events do not share one millisecond field: {sorted(prefixes)}")

        # Guard on the guard #2: id order must DISAGREE with queue order, or the
        # assertion below cannot tell the two sorts apart. Here it is the exact
        # reverse, which is the strongest disagreement available.
        assert sorted(queued) == list(reversed(queued)), (
            "id order is not the reverse of queue order, so an id-only sort "
            "would be indistinguishable from a correct one")

        # Guard on the guard #3: creation timestamps must be distinct and
        # ascending, or there is no queue order to preserve.
        assert len(set(created)) == PAIRS, (
            "events share a created_at; distinct microseconds were expected")
        assert list(created) == sorted(created), (
            "creation times are not ascending, so 'queue order' is undefined")

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
