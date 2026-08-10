"""Entry 11B5H2D — two reasons to go stale, arriving in either order.

A user's advice can be invalidated by more than one thing at once: their own
figures moved AND a rule was republished. Both queue an event, both target the
same analysis, and they are processed one after another.

THE SEMANTICS ARE FIRST-CAUSE-WINS, and they are not a convention — they fall
out of a predicate. `ScenarioFreshnessService.invalidate_for_analysis` updates
only rows still `freshness_status = 'current'`, and the relay's
`_stale_runs_for_tenant` carries the same condition. The first event to arrive
moves the row out of `current`; the second finds nothing to update and the
recorded reason is untouched.

That predicate was always correct. What was NOT correct, and is what these
tests found, is which event the relay called "first":
`ioe.claim_freshness_events` selected the batch by `created_at, id` and then
returned it `ORDER BY c.id`, and `ref.uuid_generate_v7` is a millisecond
timestamp plus randomness — so two causes a few microseconds apart were handed
to the relay in random order 48 times out of 200. The reason a user was shown
came down to a coin flip. `48_freshness_claim_order.sql` returns the batch in
the order it was selected; `test_freshness_claim_order.py` guards it directly.
These tests are the behaviour that defect corrupted.

WHY THAT IS THE RIGHT BEHAVIOUR rather than an accident worth "fixing" to
last-writer-wins: the reason is shown to a person to tell them what to do next.
"your figures moved" sends them to check their data; "a rule changed" sends
them to re-run. The FIRST cause is the one that actually invalidated the advice
they were looking at, and overwriting it with whatever happened to be processed
last would make the label describe queue ordering rather than their situation.

So the test is symmetric. A→B must record A and B→A must record B; a file that
only checked one order would also pass if the reason were decided by
alphabetical order, enum position, or which code sorts higher — none of which
is first-cause-wins.
"""
from __future__ import annotations

import uuid

import psycopg2

from app.services.ioe.freshness_relay import FreshnessRelay
from tests.conftest import owner_dsn

# Two causes a real producer emits, chosen because they tell a person to do
# DIFFERENT things — which is exactly why which one is recorded matters.
INPUTS = "BASELINE_INPUTS_CHANGED"       # financial_data_changed / profile_changed
RULES = "RULE_SNAPSHOT_SUPERSEDED"       # rule_published / rule_withdrawn


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _uuid(value) -> uuid.UUID:
    return uuid.UUID(str(value))


def _account_with_current_advice(cur) -> tuple[uuid.UUID, uuid.UUID]:
    """A scenario AND an optimization run, both `current`.

    Both are asserted on, because they are staled by two different code paths
    that each carry their own copy of the `current` predicate — the service for
    scenarios, the relay for runs. A single-writer test would leave the other
    free to adopt last-writer-wins unnoticed.
    """
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"cause_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("""
        INSERT INTO analysis.analysis_run
            (user_id, tax_year, engine_version, status, data_verified)
        VALUES (%s, 2025, 'py-1.0.0', 'completed', true)
        RETURNING id
    """, (str(user),))
    analysis = _uuid(cur.fetchone()[0])
    cur.execute("""
        INSERT INTO ioe.scenario
            (user_id, base_analysis_id, tax_year, workflow_status,
             freshness_status)
        VALUES (%s, %s, 2025, 'completed', 'current')
    """, (str(user), str(analysis)))
    cur.execute("""
        INSERT INTO ioe.optimization_run
            (user_id, analysis_id, tax_year, workflow_status, freshness_status)
        VALUES (%s, %s, 2025, 'completed', 'current')
    """, (str(user), str(analysis)))
    return user, analysis


def _queue(cur, user: uuid.UUID, analysis: uuid.UUID, reason: str,
           event_id: uuid.UUID | None = None,
           created_at: str | None = None) -> uuid.UUID:
    """Queue one cause. `event_id` and `created_at` are settable so a test can
    build the adversarial arrangement below deterministically."""
    cur.execute("""
        INSERT INTO ioe.freshness_outbox
            (id, event_type, stale_reason_code, user_id, analysis_id,
             dedupe_key, created_at)
        VALUES (coalesce(%s::uuid, ref.uuid_generate_v7()),
                'financial_data_changed', %s, %s, %s, %s,
                coalesce(%s::timestamptz, now()))
        RETURNING id
    """, (str(event_id) if event_id else None, reason, str(user), str(analysis),
          f"cause:{uuid.uuid4()}", created_at))
    return _uuid(cur.fetchone()[0])


def _adversarial_pair(cur, user, analysis, first: str, second: str):
    """Queue two causes so that queue order and id order DISAGREE.

    This is the arrangement the 0054 fix exists for. `ref.uuid_generate_v7`
    builds an id from a 48-bit millisecond timestamp plus `gen_random_bytes`
    with no monotonic counter, so two events queued in the same millisecond get
    ids in random relative order — about half the time, reversed.

    Left to chance, this test is a coin flip: measured in Entry 11B5J against
    the known-bad `ORDER BY c.id`, three consecutive runs all passed, so it was
    not protecting the defect at all. Choosing the ids makes the condition
    certain instead of likely, and it is not a contrived one — it is exactly
    what the generator produces half the time.

    `created_at` is set explicitly and distinctly, one millisecond apart, so
    that `ORDER BY created_at, id` has an unambiguous answer that equals the
    queueing order, while `ORDER BY id` alone returns them reversed.
    """
    low, high = sorted((uuid.uuid4(), uuid.uuid4()))
    # The timestamps stay in the present, so these remain the newest rows in a
    # shared queue — the realistic position, and the one that made this file
    # fail under backlog. Only the ids are chosen: FIRST gets the LATER id, so
    # an id-only sort puts SECOND ahead of it.
    cur.execute("SELECT now(), now() + interval '1 millisecond'")
    first_at, second_at = cur.fetchone()
    first_id = _queue(cur, user, analysis, first, event_id=high,
                      created_at=first_at.isoformat())
    second_id = _queue(cur, user, analysis, second, event_id=low,
                       created_at=second_at.isoformat())
    return first_id, second_id


def _scenario(cur, user: uuid.UUID) -> tuple[str, str | None]:
    cur.execute("SELECT freshness_status, stale_reason_code FROM ioe.scenario "
                " WHERE user_id = %s", (str(user),))
    return cur.fetchone()


def _run(cur, user: uuid.UUID) -> tuple[str, list[str]]:
    cur.execute("SELECT freshness_status, stale_reason_codes "
                "  FROM ioe.optimization_run WHERE user_id = %s", (str(user),))
    return cur.fetchone()


def _states(cur, events: list[uuid.UUID]) -> list[str]:
    cur.execute("SELECT claim_state FROM ioe.freshness_outbox "
                " WHERE id = ANY(%s::uuid[]) ORDER BY created_at, id",
                ([str(e) for e in events],))
    return [r[0] for r in cur.fetchall()]


#: Rounds of `drain()` before giving up. A runaway guard: the real exit is
#: "this test's own events reached a terminal state". `drain()` is itself
#: bounded at `max_passes=10` (2000 events), which is correct for production —
#: a producer emitting faster than the relay drains must not spin forever — and
#: is not a promise that one call empties a shared queue.
_MAX_DRAIN_ROUNDS = 100


async def _drain(cur=None, events: list | None = None):
    """Relay until THIS test's events are processed, not for a fixed effort.

    One `drain()` clears at most 2000 events, and these events are the newest
    in a shared oldest-first queue. Measured in Entry 11B5J: with a 3252-event
    backlog a single drain never reached them and the test failed with
    "both causes must be consumed, got ['pending', 'pending']" — while the
    relay was working exactly as designed.

    Passing the event ids makes the exit condition the thing the test actually
    needs. Without them this falls back to draining until a pass claims
    nothing.
    """
    relay = FreshnessRelay("causes")
    try:
        for _ in range(_MAX_DRAIN_ROUNDS):
            report = await relay.drain(batch_size=200)
            if events is not None and cur is not None:
                if all(st in ("completed", "failed")
                       for st in _states(cur, events)):
                    return
            elif report.claimed == 0:
                return
        raise AssertionError(
            "the relay never reached this test's events; the queue is not "
            "making progress")
    finally:
        from app.database.privacy_session import dispose_worker_engines
        from app.database.session import engine

        await engine.dispose()
        await dispose_worker_engines()


async def _first_cause_wins(first: str, second: str) -> None:
    """Queue `first` then `second`, drain, and assert `first` is recorded.

    Ordering is not left to chance: `claim_freshness_events` orders by
    `created_at, id` and the relay processes the returned batch in that order,
    so inserting in sequence IS the arrival order.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user, analysis = _account_with_current_advice(cur)

        assert _scenario(cur, user) == ("current", None), "no current scenario"
        assert _run(cur, user)[0] == "current", "no current run"

        events = list(_adversarial_pair(cur, user, analysis, first, second))

        await _drain(cur, events)

        # BOTH events must have been processed. If the second were still
        # pending, "the first reason is recorded" would be trivially true and
        # would prove nothing about a second cause arriving at all.
        assert _states(cur, events) == ["completed", "completed"], (
            f"both causes must be consumed, got {_states(cur, events)}")

        status, reason = _scenario(cur, user)
        assert status == "stale", "the scenario never went stale"
        assert reason == first, (
            f"the scenario records {reason!r}; the FIRST cause was {first!r}. "
            "A second cause overwrote the reason a user is shown.")

        run_status, run_reasons = _run(cur, user)
        assert run_status == "stale", "the optimization run never went stale"
        assert run_reasons == [first], (
            f"the run records {run_reasons}; the FIRST cause was {first!r}")
    finally:
        admin.close()


async def test_inputs_then_rules_records_the_input_change():
    await _first_cause_wins(INPUTS, RULES)


async def test_rules_then_inputs_records_the_rule_change():
    """The mirror image. Together with the case above this pins first-cause-
    wins specifically — any rule that depends on WHICH codes they are, rather
    than on their order, fails exactly one of the two."""
    await _first_cause_wins(RULES, INPUTS)


async def test_the_second_cause_is_still_consumed_not_stranded():
    """First-cause-wins must not mean the loser is left in the queue.

    An event that finds nothing to update has still been handled: it marks zero
    rows, completes, and leaves. If it instead failed — 'I changed nothing,
    therefore something is wrong' — it would be retried five times and end in
    `failed`, and a perfectly ordinary double invalidation would look like a
    queue defect to whoever reads the outbox.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user, analysis = _account_with_current_advice(cur)
        first = _queue(cur, user, analysis, INPUTS)
        second = _queue(cur, user, analysis, RULES)

        await _drain(cur, [first, second])

        cur.execute("SELECT claim_state, attempts, last_error_code "
                    "  FROM ioe.freshness_outbox WHERE id = %s", (str(second),))
        state, attempts, error_code = cur.fetchone()
        assert state == "completed", (
            f"the superseded cause ended {state!r}, not completed")
        assert attempts == 1, (
            f"the superseded cause was retried {attempts} times; marking zero "
            "rows is a normal outcome, not a failure")
        assert error_code is None, f"an error code was recorded: {error_code!r}"

        cur.execute("SELECT claim_state FROM ioe.freshness_outbox WHERE id = %s",
                    (str(first),))
        assert cur.fetchone()[0] == "completed", "the first cause did not complete"
    finally:
        admin.close()
