"""Entry 11B5H2D — two tenants doing privacy-relevant work at the same time.

Everything else in this slice follows ONE account through a purge. These tests
put a second account beside it, because the relay is the one component in the
deletion story that is genuinely cross-tenant: a single claimed batch can carry
events belonging to many users, and the relay switches `app.user_id` between
them as it walks the batch. That switching is where a leak would live.

The properties:

  * a batch spanning two tenants stales each one only inside its own scope, and
    the reason recorded for one is not the reason recorded for the other;
  * purging one account does not disturb the other's queue or its data;
  * two accounts purging concurrently both finish — the keyhole takes no lock
    that serialises unrelated tenants.

Real connections and real concurrency throughout. A cross-tenant test that
never actually interleaves proves serialisation, not isolation.
"""
from __future__ import annotations

import threading
import uuid

import psycopg2

from app.services.ioe.freshness_relay import FreshnessRelay
from tests.conftest import owner_dsn

INPUTS = "BASELINE_INPUTS_CHANGED"
RULES = "RULE_SNAPSHOT_SUPERSEDED"
PHASE = "SOURCE_DATA"


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _uuid(value) -> uuid.UUID:
    return uuid.UUID(str(value))


def _tenant(cur, amount: int) -> tuple[uuid.UUID, uuid.UUID]:
    """An account with income, an analysis, and a `current` scenario.

    `amount` differs per tenant so a leak is visible as a VALUE, not only as a
    row count — if one tenant's purge reached the other's table, the survivor's
    amount tells us which one went.
    """
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"xt_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025, id, %s, 'ON' FROM ref.income_type
         WHERE code = 'employment'
    """, (str(user), amount))
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
    return user, analysis


def _queue(cur, user: uuid.UUID, analysis: uuid.UUID, reason: str) -> uuid.UUID:
    cur.execute("""
        INSERT INTO ioe.freshness_outbox
            (event_type, stale_reason_code, user_id, analysis_id, dedupe_key)
        VALUES ('financial_data_changed', %s, %s, %s, %s)
        RETURNING id
    """, (reason, str(user), str(analysis), f"xt:{uuid.uuid4()}"))
    return _uuid(cur.fetchone()[0])


def _to_purging(cur, user: uuid.UUID) -> uuid.UUID:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'xt', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    return token


def _remaining(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    return cur.fetchone()[0]


def _scenario(cur, user: uuid.UUID) -> tuple[str, str | None]:
    cur.execute("SELECT freshness_status, stale_reason_code FROM ioe.scenario "
                " WHERE user_id = %s", (str(user),))
    return cur.fetchone()


def _amounts(cur, user: uuid.UUID) -> list[int]:
    cur.execute("SELECT amount::int FROM finance.income_source "
                " WHERE user_id = %s ORDER BY amount", (str(user),))
    return [r[0] for r in cur.fetchall()]


#: Rounds of `drain()` before giving up. A runaway guard: the real exit is
#: "this test's own event reached a terminal state". `drain()` is bounded at
#: `max_passes=10` (2000 events), which is right for production and is not a
#: promise that one call empties a queue shared with the whole suite.
_MAX_DRAIN_ROUNDS = 100


async def _drain(worker: str = "xtenant", cur=None, events=None) -> None:
    """Relay until THIS test's event is processed, not for a fixed effort.

    These events are the newest in a shared oldest-first queue. Measured in
    Entry 11B5J: against a 3252-event backlog a single drain never reached the
    bystander's event and the test failed with "the bystander's own event did
    not take effect after a neighbour was purged" — while the relay was doing
    exactly what a fair bounded queue should do.
    """
    try:
        for _ in range(_MAX_DRAIN_ROUNDS):
            report = await FreshnessRelay(worker).drain(batch_size=200)
            if events and cur is not None:
                cur.execute("SELECT claim_state FROM ioe.freshness_outbox "
                            " WHERE id = ANY(%s::uuid[])",
                            ([str(e) for e in events],))
                if all(r[0] in ("completed", "failed") for r in cur.fetchall()):
                    return
            elif report.claimed == 0:
                return
        raise AssertionError(
            "the relay never reached this test's event; the queue is not "
            "making progress")
    finally:
        from app.database.privacy_session import dispose_worker_engines
        from app.database.session import engine

        await engine.dispose()
        await dispose_worker_engines()


async def test_one_batch_two_tenants_each_staled_only_in_its_own_scope():
    """The relay switches `app.user_id` between events in a single batch.

    Both tenants' events are pending at the same time and are claimed together,
    with DIFFERENT reasons, so a scope error shows up as the wrong reason
    rather than merely as the wrong count.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        a, a_analysis = _tenant(cur, 1111)
        b, b_analysis = _tenant(cur, 2222)
        a_event = _queue(cur, a, a_analysis, INPUTS)
        b_event = _queue(cur, b, b_analysis, RULES)

        assert _scenario(cur, a) == ("current", None)
        assert _scenario(cur, b) == ("current", None)

        await _drain(cur=cur, events=[a_event, b_event])

        assert _scenario(cur, a) == ("stale", INPUTS), (
            f"tenant A recorded {_scenario(cur, a)}, expected {INPUTS}")
        assert _scenario(cur, b) == ("stale", RULES), (
            f"tenant B recorded {_scenario(cur, b)}, expected {RULES}")

        # Neither tenant's data was touched by the other's event.
        assert _amounts(cur, a) == [1111], "tenant A's income changed"
        assert _amounts(cur, b) == [2222], "tenant B's income changed"
    finally:
        admin.close()


async def test_purging_one_tenant_leaves_the_other_tenants_queue_and_data():
    """A purge is a per-subject keyhole. The neighbour must not notice it."""
    admin = _owner()
    try:
        cur = admin.cursor()
        doomed, doomed_analysis = _tenant(cur, 1111)
        bystander, bystander_analysis = _tenant(cur, 2222)
        _queue(cur, doomed, doomed_analysis, INPUTS)
        bystander_event = _queue(cur, bystander, bystander_analysis, RULES)

        token = _to_purging(cur, doomed)
        cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'xt')",
                    (str(doomed), PHASE, str(token)))
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'xt')",
                    (str(doomed), str(token)))

        assert _remaining(cur, doomed) == 0, "the purge did not run"
        assert _amounts(cur, bystander) == [2222], (
            "the purge reached across into another tenant's income")
        assert _remaining(cur, bystander) > 0, (
            "the bystander's source data was counted away by another "
            "account's purge")

        # The bystander's queued event is untouched and still works.
        cur.execute("SELECT claim_state FROM ioe.freshness_outbox WHERE id = %s",
                    (str(bystander_event),))
        assert cur.fetchone()[0] == "pending", (
            "another account's purge disturbed this tenant's queue")

        await _drain(cur=cur, events=[bystander_event])

        assert _scenario(cur, bystander) == ("stale", RULES), (
            "the bystander's own event did not take effect after a neighbour "
            "was purged")
        assert _amounts(cur, bystander) == [2222], "the bystander lost data"
    finally:
        admin.close()


def test_two_accounts_purge_concurrently_without_serialising():
    """Both purges run at once, from independent connections.

    The failure this guards against is a keyhole that takes an account-wide or
    table-wide lock: correctness would survive, but every deletion in the system
    would queue behind every other. Both must reach zero, and both phases must
    be certifiable afterwards.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        a, _ = _tenant(cur, 1111)
        b, _ = _tenant(cur, 2222)
        a_token = _to_purging(cur, a)
        b_token = _to_purging(cur, b)
        for user, token in ((a, a_token), (b, b_token)):
            cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'xt')",
                        (str(user), PHASE, str(token)))

        start = threading.Barrier(2)
        errors: list[BaseException] = []

        def purge(user: uuid.UUID, token: uuid.UUID) -> None:
            conn = psycopg2.connect(owner_dsn())
            conn.autocommit = True
            try:
                start.wait(timeout=10)   # both inside the function together
                conn.cursor().execute(
                    "SELECT identity.purge_source_data(%s, %s, 'xt')",
                    (str(user), str(token)))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=purge, args=(a, a_token)),
                   threading.Thread(target=purge, args=(b, b_token))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), (
            "a purge did not finish within 30s — two unrelated tenants are "
            "serialising on each other")
        assert not errors, f"a concurrent purge failed: {errors}"

        assert _remaining(cur, a) == 0, "tenant A was not purged"
        assert _remaining(cur, b) == 0, "tenant B was not purged"
        for user, token in ((a, a_token), (b, b_token)):
            cur.execute(
                "SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'xt')",
                (str(user), PHASE, str(token)))
            assert cur.fetchone()[0], f"the phase for {user} could not complete"
    finally:
        admin.close()
