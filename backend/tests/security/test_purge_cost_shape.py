"""Entry 11B5I — the shape of deletion cost, guarded where it can regress.

`scripts/probe_source_purge_cost.py` reports the measurements. This file guards
the property those measurements establish, and deliberately guards only the part
that is a property of the CODE:

    the application issues a BOUNDED number of round trips,
    independent of how many rows the account has.

Latency is not asserted. A millisecond threshold measured on a local container
would fail on a loaded CI runner for reasons that have nothing to do with the
code, and §37 is explicit that p50/p95 stay evidence rather than gates. What
cannot drift without a real defect is the statement count: the day someone
replaces the set-based purge with a loop, this fails at 1000 rows and passes at
1, which is exactly the signature of the defect.

Measured on a local development database at 222aa97, for context:

    rows   application statements   p50        freshness events
       1                        1   1.16 ms                   0
      10                        1   3.24 ms                   0
     100                        1  17.53 ms                   0
    1000                        1 141.79 ms                   0

count_remaining_source_data: 1 statement at every scale, p50 0.30–0.41 ms.
Repeat purge after the rows are gone: 0.97 ms at 1000 rows.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PHASE = "SOURCE_DATA"

#: Small enough to stay fast in CI, far enough apart that an O(rows)
#: implementation cannot produce the same count at both ends.
SMALL, LARGE = 1, 400


class CountingCursor(psycopg2.extensions.cursor):
    """Counts application round trips. psycopg2 will not accept an assigned
    `execute`, so the count is taken by subclassing."""

    count = 0

    def execute(self, sql, args=None):
        CountingCursor.count += 1
        return super().execute(sql, args)


def _owner():
    conn = psycopg2.connect(owner_dsn(), cursor_factory=CountingCursor)
    conn.autocommit = True
    return conn


def _account(cur) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"cost_{uuid.uuid4().hex[:12]}@example.com"))
    return user


def _compose(cur, user: uuid.UUID, rows: int) -> None:
    """Spread across governed tables and both tax-year partitions, so `rows`
    means a real purge workload rather than one DELETE against one table."""
    cur.execute("INSERT INTO profile.tax_profile (user_id, province_code, "
                "marital_status) VALUES (%s, 'ON', 'single')", (str(user),))
    half = max((rows - 1) // 2, 0)
    if half:
        cur.execute("""
            INSERT INTO finance.income_source
                (user_id, tax_year, income_type_id, amount, province_code)
            SELECT %s, 2024 + (g %% 2),
                   (SELECT id FROM ref.income_type WHERE code = 'employment'),
                   1000 + g, 'ON' FROM generate_series(1, %s) g
        """, (str(user), half))
        cur.execute("""
            INSERT INTO finance.expense_record
                (user_id, tax_year, expense_category_id, amount)
            SELECT %s, 2024 + (g %% 2),
                   (SELECT id FROM ref.expense_category LIMIT 1), 10 + g
              FROM generate_series(1, %s) g
        """, (str(user), half))


def _to_purging(cur, user: uuid.UUID) -> uuid.UUID:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'cost', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'cost')",
                (str(user), PHASE, str(token)))
    return token


def _remaining(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    return cur.fetchone()[0]


def _purge_round_trips(cur, user: uuid.UUID, token: uuid.UUID) -> int:
    start = CountingCursor.count
    cur.execute("SELECT identity.purge_source_data(%s, %s, 'cost')",
                (str(user), str(token)))
    return CountingCursor.count - start


# ------------------------------------------------------------------- §12/13 --
def test_the_account_purge_costs_the_same_number_of_round_trips_at_any_size():
    """The O(rows) guard. One subject with 1 qualifying row and one with 400
    must cost the application exactly the same number of calls."""
    admin = _owner()
    try:
        cur = admin.cursor()
        counts = {}
        for scale in (SMALL, LARGE):
            user = _account(cur)
            _compose(cur, user, scale)
            token = _to_purging(cur, user)

            before = _remaining(cur, user)
            counts[scale] = (_purge_round_trips(cur, user, token), before)

            assert _remaining(cur, user) == 0, f"purge left rows at scale {scale}"
            cur.execute(
                "SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'cost')",
                (str(user), PHASE, str(token)))
            assert cur.fetchone()[0], f"completion refused at scale {scale}"

        small_calls, small_rows = counts[SMALL]
        large_calls, large_rows = counts[LARGE]

        # Guard on the guard: the two scales must actually differ in row count,
        # or "the counts match" is trivially true.
        assert large_rows > small_rows * 50, (
            f"the fixture did not build meaningfully different accounts: "
            f"{small_rows} vs {large_rows} qualifying rows")

        assert small_calls == large_calls == 1, (
            f"the account purge cost {small_calls} call(s) for {small_rows} rows "
            f"and {large_calls} for {large_rows}. It must be one set-based call "
            "at any size; a per-row loop is the defect this guards.")
    finally:
        admin.close()


# ---------------------------------------------------------------------- §14 --
@pytest.mark.parametrize("scale", [SMALL, LARGE])
def test_the_completeness_check_is_one_round_trip_at_any_size(scale):
    """`count_remaining_source_data` is the completion authority, so it runs on
    every phase attempt. It must not become chatty as an account grows."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _compose(cur, user, scale)

        start = CountingCursor.count
        remaining = _remaining(cur, user)
        calls = CountingCursor.count - start

        assert remaining >= 1, "the fixture created nothing to count"
        assert calls == 1, (
            f"counting {remaining} qualifying rows took {calls} application "
            "calls; it must be one")
    finally:
        admin.close()


# ---------------------------------------------------------------------- §15 --
def test_repeating_a_purge_after_the_rows_are_gone_stays_cheap():
    """Idempotent retry is production behaviour — a worker that crashed after
    purging repeats the whole phase. Zero rows deleted is a normal outcome, and
    it must not cost more than the first run."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _compose(cur, user, LARGE)
        token = _to_purging(cur, user)

        first = _purge_round_trips(cur, user, token)
        assert _remaining(cur, user) == 0, "the first purge did not clear the rows"

        repeat = _purge_round_trips(cur, user, token)
        assert _remaining(cur, user) == 0, "the repeat purge changed the result"
        assert repeat == first == 1, (
            f"the repeat purge cost {repeat} call(s) against {first} for the "
            "first; retry must stay bounded")
    finally:
        admin.close()


# ---------------------------------------------------------------------- §17 --
@pytest.mark.parametrize("scale", [SMALL, LARGE])
def test_the_purge_emits_no_freshness_event_at_any_size(scale):
    """The H2 policy, re-checked as a SCALING property: if the purge ever began
    emitting per deleted row, this is where it would show up as O(rows) queue
    traffic describing an account nobody may look at again."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _compose(cur, user, scale)
        token = _to_purging(cur, user)

        cur.execute("SELECT count(*) FROM ioe.freshness_outbox WHERE user_id = %s",
                    (str(user),))
        before = cur.fetchone()[0]

        cur.execute("SELECT identity.purge_source_data(%s, %s, 'cost')",
                    (str(user), str(token)))

        cur.execute("SELECT count(*) FROM ioe.freshness_outbox WHERE user_id = %s",
                    (str(user),))
        assert cur.fetchone()[0] == before == 0, (
            f"the purge emitted freshness events at scale {scale}")
    finally:
        admin.close()


# ---------------------------------------------------------------------- §18 --
def test_the_worker_claim_batch_stays_bounded_as_subjects_accumulate():
    """A backlog must not turn one claim into an unbounded batch. The cap lives
    in SQL (`least(greatest(coalesce(p_batch_size, 1), 1), 50)`), so a caller
    asking for more than the cap still gets at most the cap."""
    admin = _owner()
    try:
        cur = admin.cursor()
        for _ in range(60):
            user = _account(cur)
            cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                        "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))

        start = CountingCursor.count
        cur.execute("SELECT count(*) FROM "
                    "identity.claim_account_lifecycle(10000, 'cost-worker')")
        claimed = cur.fetchone()[0]
        calls = CountingCursor.count - start

        assert calls == 1, f"claiming took {calls} application calls"
        assert 0 < claimed <= 50, (
            f"a claim asking for 10000 returned {claimed}; the SQL cap of 50 is "
            "not holding, and a backlog could be swallowed whole")
    finally:
        admin.close()
