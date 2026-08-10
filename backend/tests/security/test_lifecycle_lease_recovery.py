"""Entry 11B5H2E — a privacy worker that dies mid-deletion, and the lease.

Entry 11B5H1 proved the claim-token rules using state set up by hand. This file
drives the SAME rules through the real lease path, because the two are not the
same claim: hand-setting `claimed_by = NULL` tests the token check, while
letting `identity.claim_account_lifecycle` expire and recover a claim tests the
mechanism that actually runs when a worker disappears.

The mechanism, read from `41_account_lifecycle.sql` and confirmed against the
installed `pg_proc` body (46 widened the predicate, so 41's text alone would be
out of date):

  * `identity.lifecycle_claim_timeout()` = 10 minutes;
  * `claim_account_lifecycle` FIRST releases any claim with
    `claimed_at < now() - lifecycle_claim_timeout()`, writing a
    `CLAIM_RECOVERED` / `CLAIM_EXPIRED` event for each;
  * it THEN claims where `claimed_by IS NULL` and state is one of
    DELETION_REQUESTED, ACCESS_DISABLED, PURGE_PENDING, PURGING,
    FAILED_RETRYABLE, `ORDER BY requested_at ... FOR UPDATE SKIP LOCKED`;
  * the `claim_token` it issues is the authority for `purge_source_data`,
    `complete_lifecycle_phase` and `fail_lifecycle_phase`.

Expiry is reached by ageing `claimed_at`, which is the one thing that cannot be
done by waiting inside a test. Everything else — who wins, who is refused, what
converges — is left to the real functions.
"""
from __future__ import annotations

import threading
import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PHASE = "SOURCE_DATA"


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _account(cur, *, rows: int = 2) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"lease_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("INSERT INTO profile.tax_profile (user_id, province_code, "
                "marital_status) VALUES (%s, 'ON', 'single')", (str(user),))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025,
               (SELECT id FROM ref.income_type WHERE code = 'employment'),
               g * 1000, 'ON'
          FROM generate_series(1, %s) g
    """, (str(user), rows))
    return user


def _to_purge_pending(cur, user: uuid.UUID) -> None:
    """Walk the real transition chain to a claimable purge state."""
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))


def _claim(cur, worker: str, user: uuid.UUID) -> uuid.UUID | None:
    """Claim through the real function, returning THIS account's token.

    The queue is shared with every other test, so the batch is taken wide and
    filtered rather than assumed to contain one row.
    """
    cur.execute("SELECT out_user_id, out_claim_token "
                "  FROM identity.claim_account_lifecycle(50, %s)", (worker,))
    for claimed_user, token in cur.fetchall():
        if str(claimed_user) == str(user):
            return token
    return None


def _age_claim(cur, user: uuid.UUID, *, past: bool = True) -> None:
    """Push `claimed_at` beyond the lease, using the function's own timeout so
    a change to the interval cannot make these tests stop reaching recovery."""
    delta = "+ interval '1 minute'" if past else "- interval '1 minute'"
    cur.execute(
        "UPDATE identity.account_lifecycle "
        "   SET claimed_at = now() - identity.lifecycle_claim_timeout() "
        f"                 {'-' if past else '+'} interval '1 minute' "
        " WHERE user_id = %s", (str(user),))
    _ = delta


def _claim_is_expired(cur, user: uuid.UUID) -> bool:
    """§17 — the guard on the guard, evaluated by the DATABASE.

    A reclaim test that never proves the previous claim was expired may simply
    be exercising ordinary claim logic against a released row. This asks
    PostgreSQL the same question `claim_account_lifecycle` asks.
    """
    cur.execute("""
        SELECT claimed_by IS NOT NULL
           AND claimed_at < now() - identity.lifecycle_claim_timeout()
          FROM identity.account_lifecycle WHERE user_id = %s
    """, (str(user),))
    return bool(cur.fetchone()[0])


def _state(cur, user: uuid.UUID) -> str:
    cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id = %s",
                (str(user),))
    return cur.fetchone()[0]


def _remaining(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    return cur.fetchone()[0]


def _phase_status(cur, user: uuid.UUID) -> str | None:
    cur.execute("SELECT status FROM identity.account_lifecycle_phase "
                " WHERE user_id = %s AND phase = %s", (str(user), PHASE))
    row = cur.fetchone()
    return row[0] if row else None


def _recovered_events(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT count(*) FROM identity.account_lifecycle_event "
                " WHERE user_id = %s AND event_code = 'CLAIM_RECOVERED'",
                (str(user),))
    return cur.fetchone()[0]


# ----------------------------------------------------------- §16, §17 -------
def test_a_worker_that_dies_before_purging_is_replaced_through_the_lease():
    """Worker A claims and starts SOURCE_DATA, then disappears without purging.
    Only the lease can unstick it."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _to_purge_pending(cur, user)

        a_token = _claim(cur, "worker-a", user)
        assert a_token is not None, "worker A did not claim the account"
        cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'worker-a')",
                    (str(user), PHASE, str(a_token)))
        assert _phase_status(cur, user) == "RUNNING", "the phase did not start"
        assert _remaining(cur, user) > 0, "nothing to purge; the test is empty"

        # A disappears. Nothing is reset by hand — only the clock moves.
        _age_claim(cur, user)
        assert _claim_is_expired(cur, user), (
            "worker A's claim is not expired under the database's own "
            "condition, so a successful reclaim below would prove nothing")

        b_token = _claim(cur, "worker-b", user)
        assert b_token is not None, "worker B could not reclaim an expired lease"
        assert b_token != a_token, "the reclaim reissued the same token"
        assert _recovered_events(cur, user) == 1, "recovery was not recorded"

        # B is authoritative: it can purge and complete.
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'worker-b')",
                    (str(user), str(b_token)))
        assert _remaining(cur, user) == 0, "worker B's purge did not run"
        cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'worker-b')",
                    (str(user), PHASE, str(b_token)))
        assert cur.fetchone()[0], "worker B could not complete the phase"
        assert _phase_status(cur, user) == "COMPLETE"
    finally:
        admin.close()


def test_a_live_claim_is_not_reclaimable_before_the_lease_expires():
    """The negative half, without which every reclaim test above is suspect.

    If a second worker could take the account whether or not the lease had
    expired, those tests would be measuring ordinary claim logic and the word
    'recovery' would mean nothing. The ONLY difference here is the clock.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _to_purge_pending(cur, user)
        a_token = _claim(cur, "worker-a", user)
        assert a_token is not None, "worker A did not claim the account"

        # Held, not expired — the mirror image of every test above.
        assert not _claim_is_expired(cur, user), "the fresh claim already expired"

        assert _claim(cur, "worker-b", user) is None, (
            "a second worker took an account whose lease was still live")
        assert _recovered_events(cur, user) == 0, (
            "a recovery was recorded for a claim that had not expired")
        cur.execute("SELECT claimed_by FROM identity.account_lifecycle "
                    " WHERE user_id = %s", (str(user),))
        assert cur.fetchone()[0] == "worker-a", "the live claimant was displaced"

        # And now the only thing that changes is the clock.
        _age_claim(cur, user)
        assert _claim(cur, "worker-b", user) is not None, (
            "the account became unclaimable rather than recoverable")
    finally:
        admin.close()


# ----------------------------------------------------------------- §18 ------
def test_the_replaced_worker_cannot_mutate_anything_with_its_stale_token():
    """A comes back after B took over. Its token must buy nothing."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _to_purge_pending(cur, user)
        a_token = _claim(cur, "worker-a", user)
        cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'worker-a')",
                    (str(user), PHASE, str(a_token)))

        _age_claim(cur, user)
        assert _claim_is_expired(cur, user), "the lease did not expire"
        b_token = _claim(cur, "worker-b", user)
        assert b_token is not None and b_token != a_token

        before = _remaining(cur, user)
        assert before > 0, "nothing left to protect; the test is empty"

        # A returns and tries every mutation its token used to authorise.
        # The purge keyhole REFUSES LOUDLY rather than quietly doing nothing —
        # a silent no-op would let a crashed worker believe it had finished.
        with pytest.raises(psycopg2.Error) as refusal:
            cur.execute("SELECT identity.purge_source_data(%s, %s, 'worker-a')",
                        (str(user), str(a_token)))
        assert "claim token does not hold this subject" in str(refusal.value)
        admin.rollback()
        assert _remaining(cur, user) == before, (
            "a worker whose lease expired still purged the account")

        cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'worker-a')",
                    (str(user), PHASE, str(a_token)))
        assert cur.fetchone()[0] is False, "a stale claimant completed the phase"

        cur.execute("SELECT identity.fail_lifecycle_phase(%s, %s, %s, %s, 'worker-a')",
                    (str(user), PHASE, str(a_token), "SOURCE_DATA_INCOMPLETE"))
        assert cur.fetchone()[0] is False, "a stale claimant failed the phase"

        assert _phase_status(cur, user) == "RUNNING", (
            "a stale claimant moved the phase out from under the live worker")

        # B is still authoritative.
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'worker-b')",
                    (str(user), str(b_token)))
        assert _remaining(cur, user) == 0, "the live claimant lost its authority"
    finally:
        admin.close()


# ----------------------------------------------------- §19, §20, §21 --------
def test_a_worker_that_dies_after_purging_converges_on_retry():
    """The crash window nobody designs for: the deletes committed, the phase
    was never marked COMPLETE. The replacement must converge, not double-act.

    A frozen analysis snapshot is in the fixture so §21 can check the sealed
    artifact is untouched by the purge, the crash and the retry.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)

        # A sealed historical artifact that the purge must leave alone.
        cur.execute("""
            INSERT INTO analysis.analysis_run
                (user_id, tax_year, engine_version, status, data_verified)
            VALUES (%s, 2025, 'py-1.0.0', 'completed', true) RETURNING id
        """, (str(user),))
        analysis = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO analysis.analysis_input_snapshot
                (analysis_id, snapshot, snapshot_hash)
            VALUES (%s, '{"employment_income": "3000", "year": 2025}'::jsonb,
                    'sealed-hash-h2e')
        """, (str(analysis),))
        cur.execute("SELECT snapshot::text, snapshot_hash "
                    "  FROM analysis.analysis_input_snapshot WHERE analysis_id = %s",
                    (str(analysis),))
        sealed_before = cur.fetchone()

        _to_purge_pending(cur, user)
        a_token = _claim(cur, "worker-a", user)
        cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'worker-a')",
                    (str(user), PHASE, str(a_token)))
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'worker-a')",
                    (str(user), str(a_token)))
        assert _remaining(cur, user) == 0, "worker A's purge did not run"
        # ...and A dies HERE, before complete_lifecycle_phase.
        assert _phase_status(cur, user) == "RUNNING", (
            "the phase was completed, so there is no crash window to test")

        _age_claim(cur, user)
        assert _claim_is_expired(cur, user), "the lease did not expire"
        b_token = _claim(cur, "worker-b", user)
        assert b_token is not None, "worker B could not reclaim after the crash"

        # B repeats the whole phase blindly, as a restarted worker would.
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'worker-b')",
                    (str(user), str(b_token)))
        assert _remaining(cur, user) == 0, "the repeated purge changed the result"
        cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'worker-b')",
                    (str(user), PHASE, str(b_token)))
        assert cur.fetchone()[0], "worker B could not complete after recovery"

        # §20 — the live data really is gone, and the account is not pretending
        # to be finished when later phases have not run.
        assert _remaining(cur, user) == 0
        assert _phase_status(cur, user) == "COMPLETE"
        assert _state(cur, user) != "COMPLETE", (
            "the account reached COMPLETE though only SOURCE_DATA has run")
        for table, column in (("finance.income_source", "user_id"),
                              ("profile.tax_profile", "user_id")):
            cur.execute(f"SELECT count(*) FROM {table} WHERE {column} = %s",
                        (str(user),))
            assert cur.fetchone()[0] == 0, f"{table} still holds live rows"

        # §21 — the sealed artifact is byte-identical through all of it.
        cur.execute("SELECT snapshot::text, snapshot_hash "
                    "  FROM analysis.analysis_input_snapshot WHERE analysis_id = %s",
                    (str(analysis),))
        assert cur.fetchone() == sealed_before, (
            "the sealed snapshot changed during purge/crash/retry")

        # One phase row, not one per attempt.
        cur.execute("SELECT count(*) FROM identity.account_lifecycle_phase "
                    " WHERE user_id = %s AND phase = %s", (str(user), PHASE))
        assert cur.fetchone()[0] == 1, "the retry duplicated the phase record"
    finally:
        admin.close()


# ----------------------------------------------------------------- §22 ------
def test_two_workers_racing_for_an_expired_lease_produce_one_winner():
    """B and C reclaim concurrently on independent connections. The claim's own
    `FOR UPDATE SKIP LOCKED` must hand the account to exactly one."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _to_purge_pending(cur, user)
        a_token = _claim(cur, "worker-a", user)
        assert a_token is not None
        _age_claim(cur, user)
        assert _claim_is_expired(cur, user), "the lease did not expire"

        start = threading.Barrier(2)
        tokens: dict[str, uuid.UUID | None] = {}
        errors: list[BaseException] = []

        def contend(name: str) -> None:
            conn = psycopg2.connect(owner_dsn())
            conn.autocommit = True
            try:
                c = conn.cursor()
                start.wait(timeout=15)
                tokens[name] = _claim(c, name, user)
            except BaseException as exc:            # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=contend, args=("worker-b",)),
                   threading.Thread(target=contend, args=("worker-c",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), "a contender never finished"
        assert not errors, f"a contender failed: {errors}"

        winners = [name for name, token in tokens.items() if token is not None]
        assert len(winners) == 1, (
            f"expected exactly one new claimant, got {winners}")
        winner = winners[0]
        loser = next(n for n in ("worker-b", "worker-c") if n != winner)

        # The loser holds nothing and can mutate nothing.
        cur.execute("SELECT claimed_by FROM identity.account_lifecycle "
                    " WHERE user_id = %s", (str(user),))
        assert cur.fetchone()[0] == winner, "the lifecycle names the wrong claimant"

        before = _remaining(cur, user)
        with pytest.raises(psycopg2.Error) as refusal:
            cur.execute("SELECT identity.purge_source_data(%s, %s, %s)",
                        (str(user), str(uuid.uuid4()), loser))
        assert "claim token does not hold this subject" in str(refusal.value)
        admin.rollback()
        assert _remaining(cur, user) == before, "the losing contender purged"
    finally:
        admin.close()


# ----------------------------------------------------------------- §23 ------
def test_completion_authority_follows_the_lease_not_the_order_of_arrival():
    """H1 proved this against hand-built state. Here the takeover happens
    through real expiry, and completion is still refused until the count is
    actually zero — so 'B may complete' is not a free pass either."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)
        _to_purge_pending(cur, user)
        a_token = _claim(cur, "worker-a", user)
        cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'worker-a')",
                    (str(user), PHASE, str(a_token)))

        _age_claim(cur, user)
        assert _claim_is_expired(cur, user), "the lease did not expire"
        b_token = _claim(cur, "worker-b", user)
        assert b_token is not None

        # A: refused outright.
        cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'worker-a')",
                    (str(user), PHASE, str(a_token)))
        assert cur.fetchone()[0] is False, "the displaced worker completed the phase"

        # B: holds the lease, but has not purged yet — still refused, by the
        # completeness gate rather than by the token.
        assert _remaining(cur, user) > 0, "nothing remains; the gate is untested"
        refused = False
        try:
            cur.execute(
                "SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'worker-b')",
                (str(user), PHASE, str(b_token)))
            refused = cur.fetchone()[0] is False
        except psycopg2.Error:
            refused = True                  # the gate raises rather than returns
        assert refused, (
            "the live claimant completed SOURCE_DATA while source rows remained")
        assert _phase_status(cur, user) != "COMPLETE"

        # Only after the purge.
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'worker-b')",
                    (str(user), str(b_token)))
        cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'worker-b')",
                    (str(user), PHASE, str(b_token)))
        assert cur.fetchone()[0], "completion was refused after a clean purge"
        assert _phase_status(cur, user) == "COMPLETE"
    finally:
        admin.close()
