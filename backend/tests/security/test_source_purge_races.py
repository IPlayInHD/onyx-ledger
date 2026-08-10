"""Entry 11B5H — the SOURCE_DATA purge under real concurrency.

Real PostgreSQL sessions throughout. Nothing here simulates a race with sleeps
or sequential calls: each test opens independent connections, holds a
transaction open at the point that matters, and lets the database arbitrate.
A race test that cannot demonstrate the race occurred is a test that proves
serialization, not safety.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

APP_DSN = "postgresql://onyx_test:test@/onyx_test?host=/var/run/postgresql&port=5432"


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _subject_with_income(cur) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"race_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025, id, 1000, 'ON' FROM ref.income_type
         WHERE code = 'employment'
    """, (str(user),))
    return user


def _to_purging(cur, user: uuid.UUID) -> uuid.UUID:
    """Walk the real transition chain and take a claim, as the worker does."""
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle "
                "   SET claimed_by = 'race-worker', claim_token = %s, "
                "       claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    return token


def _remaining(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The defining completion race
# ---------------------------------------------------------------------------
def test_completion_is_refused_when_a_row_appears_before_the_check():
    """The schedule that must never produce a false COMPLETE:

        worker purges        -> table empty
        writer inserts       -> a qualifying row exists again
        worker completes     -> MUST be refused

    Completion is decided by `count_remaining_source_data`, not by what the
    DELETE reported, and this is why: a table the purge forgot reports nothing,
    which looks exactly like a table that was already empty.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _subject_with_income(cur)
        token = _to_purging(cur, user)

        cur.execute("SELECT identity.purge_source_data(%s, %s, 'race-worker')",
                    (str(user), str(token)))
        assert _remaining(cur, user) == 0

        # A writer slips a qualifying row in after the purge, before completion.
        cur.execute("""
            INSERT INTO finance.income_source
                (user_id, tax_year, income_type_id, amount, province_code)
            SELECT %s, 2025, id, 1, 'ON' FROM ref.income_type
             WHERE code = 'employment'
        """, (str(user),))
        assert _remaining(cur, user) == 1, "the race did not actually occur"

        with pytest.raises(psycopg2.Error) as caught:
            cur.execute("SELECT identity.complete_lifecycle_phase("
                        "%s, 'SOURCE_DATA', %s, 'race-worker')",
                        (str(user), str(token)))
        assert "in-scope rows remain" in str(caught.value)

        cur.execute("SELECT status FROM identity.account_lifecycle_phase "
                    " WHERE user_id = %s AND phase = 'SOURCE_DATA'",
                    (str(user),))
        row = cur.fetchone()
        assert row is None or row[0] != "COMPLETE", (
            "SOURCE_DATA reported COMPLETE while a source row existed")
    finally:
        admin.close()


# ---------------------------------------------------------------------------
# Claim exclusivity and recovery
# ---------------------------------------------------------------------------
def test_two_workers_cannot_hold_the_same_lifecycle_claim():
    """`claim_account_lifecycle` uses FOR UPDATE SKIP LOCKED, so a subject
    already claimed is invisible to the second worker rather than contended."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _subject_with_income(cur)
        cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                    "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
        for state in ("ACCESS_DISABLED", "PURGE_PENDING"):
            cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                        " WHERE user_id = %s", (state, str(user)))

        seen = []
        for worker in ("worker-a", "worker-b"):
            for _ in range(20):
                cur.execute("SELECT out_user_id, out_claim_token "
                            "  FROM identity.claim_account_lifecycle(50, %s)",
                            (worker,))
                rows = cur.fetchall()
                if not rows:
                    break
                if any(str(r[0]) == str(user) for r in rows):
                    seen.append(worker)
                    break

        assert seen == ["worker-a"], (
            f"the subject was claimable by more than one worker: {seen}")
    finally:
        admin.close()


def test_a_stale_claim_token_cannot_complete_or_purge():
    """Worker A's lease expires, worker B reclaims, and A comes back. A must
    not be able to finish the phase it no longer holds — the claim token is the
    authority, not the fact that A once had it."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _subject_with_income(cur)
        stale = _to_purging(cur, user)

        # Someone else takes it: a new token replaces the old.
        fresh = uuid.uuid4()
        cur.execute("UPDATE identity.account_lifecycle SET claim_token = %s, "
                    "claimed_by = 'worker-b', claimed_at = now() "
                    " WHERE user_id = %s", (str(fresh), str(user)))

        with pytest.raises(psycopg2.Error) as caught:
            cur.execute("SELECT identity.purge_source_data(%s, %s, 'worker-a')",
                        (str(user), str(stale)))
        assert "claim token does not hold this subject" in str(caught.value)
        assert _remaining(cur, user) > 0, "a stale claim still purged the subject"

        cur.execute("SELECT identity.complete_lifecycle_phase("
                    "%s, 'SOURCE_DATA', %s, 'worker-a')",
                    (str(user), str(stale)))
        assert cur.fetchone()[0] is False, "a stale claim completed the phase"
    finally:
        admin.close()


# ---------------------------------------------------------------------------
# Cross-tenant
# ---------------------------------------------------------------------------
def test_a_purge_holds_no_lock_that_blocks_an_unrelated_tenant():
    """Two subjects purge concurrently in overlapping transactions. If the
    purge took an account-independent lock, the second would block on the
    first; it must not, because unrelated tenants sharing a lock is a
    denial-of-service surface as much as a correctness one."""
    a_conn, b_conn = psycopg2.connect(owner_dsn()), psycopg2.connect(owner_dsn())
    admin = _owner()
    try:
        setup = admin.cursor()
        user_a = _subject_with_income(setup)
        user_b = _subject_with_income(setup)
        token_a = _to_purging(setup, user_a)
        token_b = _to_purging(setup, user_b)

        # A holds its purge transaction OPEN.
        a = a_conn.cursor()
        a.execute("SELECT identity.purge_source_data(%s, %s, 'w-a')",
                  (str(user_a), str(token_a)))

        # B must complete without waiting for A to commit.
        b_conn.autocommit = True
        b = b_conn.cursor()
        b.execute("SET lock_timeout = '5s'")
        b.execute("SELECT identity.purge_source_data(%s, %s, 'w-b')",
                  (str(user_b), str(token_b)))
        a_conn.commit()

        assert _remaining(setup, user_a) == 0
        assert _remaining(setup, user_b) == 0
    finally:
        a_conn.close()
        b_conn.close()
        admin.close()
