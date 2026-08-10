"""Entry 11B5H2D — the purge and the relay, interleaved at each seam.

The relay processes one event in three separable steps, and they are separable
on purpose: CLAIM through the privileged keyhole, APPLY in an ordinary
RLS-scoped tenant transaction, ACKNOWLEDGE back through the keyhole. Each step
commits on its own, so an account's SOURCE_DATA purge can land in any gap
between them.

`test_purge_freshness_convergence` settled what happens to the EVENT: bounded
attempts, then terminal. This file asks the other half, about the DATA — does
any ordering leave the purge incomplete, bring a purged row back, or wedge the
deletion lifecycle so the phase can never be marked COMPLETE?

The completion check is the assertion that carries the weight.
`identity.count_remaining_source_data` is the authoritative predicate and
`complete_lifecycle_phase` REFUSES while it is non-zero, so "the phase
completes" is not a restatement of "nothing came back" — it is the database
agreeing, through the same function the privacy worker calls.

Each test drives the real `FreshnessRelay` and injects the purge by wrapping
the relay method that follows the seam under test, so the ordering exercised is
the relay's own rather than a re-implementation of it here.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from app.services.ioe.freshness_relay import ERROR_TENANT_APPLY_FAILED, FreshnessRelay
from tests.conftest import owner_dsn

PHASE = "SOURCE_DATA"


@pytest.fixture(autouse=True)
async def _dispose_engines():
    """Both runtimes, because the relay uses both.

    Pooled connections are bound to the event loop that opened them, and a
    per-test loop makes a survivor from an earlier test unusable. The freshness
    runtime is a SECOND engine, so disposing only the app engine — the habit
    from before Entry 11B5E5 — leaves exactly the trap it was meant to close.
    """
    yield
    from app.database.privacy_session import dispose_worker_engines
    from app.database.session import engine

    await engine.dispose()
    await dispose_worker_engines()


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _uuid(value) -> uuid.UUID:
    """psycopg2 hands back uuid columns as `str` unless `register_uuid` is
    called; the relay deals in `uuid.UUID`. Comparing the two silently yields
    False, which turned an earlier version of these tests into no-ops that
    reported success — the seam injection simply never matched its event."""
    return uuid.UUID(str(value))


def _account(cur) -> tuple[uuid.UUID, uuid.UUID]:
    """A user with purgeable source data AND a sealed run to be staled.

    The run is what makes the apply step observable: without something in
    `current` state, "the apply happened" and "the apply did nothing" look
    identical and every ordering would pass.
    """
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"seam_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025, id, 4321, 'ON' FROM ref.income_type
         WHERE code = 'employment'
    """, (str(user),))
    cur.execute("""
        INSERT INTO analysis.analysis_run
            (user_id, tax_year, engine_version, status, data_verified)
        VALUES (%s, 2025, 'py-1.0.0', 'completed', true)
        RETURNING id
    """, (str(user),))
    analysis = _uuid(cur.fetchone()[0])
    cur.execute("""
        INSERT INTO ioe.optimization_run
            (user_id, analysis_id, tax_year, workflow_status, freshness_status)
        VALUES (%s, %s, 2025, 'completed', 'current')
    """, (str(user), str(analysis)))
    return user, analysis


def _queue_event(cur, user: uuid.UUID, analysis: uuid.UUID) -> uuid.UUID:
    """Scoped to the analysis, not the year, and carrying a REAL reason code.

    `freshness_outbox_scope_is_singular` allows at most one of
    (analysis_id, tax_year), and the analysis scope is the narrower branch of
    the relay: it stales exactly this account's one run rather than fanning
    across a whole year of tenants.

    The reason code has to be one a producer actually emits. The column is
    plain `text` in the database, but the relay resolves it through
    `StaleReason(code)`, so an invented code raises inside the apply and the
    test then measures a rejected enum instead of the interleaving it claims to
    measure. `financial_data_changed` maps to BASELINE_INPUTS_CHANGED in
    `EVENT_STALE_REASON`, which is what a real income or expense edit queues.
    """
    cur.execute("""
        INSERT INTO ioe.freshness_outbox
            (event_type, stale_reason_code, user_id, analysis_id, dedupe_key)
        VALUES ('financial_data_changed', 'BASELINE_INPUTS_CHANGED', %s, %s, %s)
        RETURNING id
    """, (str(user), str(analysis), f"seam:{uuid.uuid4()}"))
    return _uuid(cur.fetchone()[0])


def _claim_token(cur, user: uuid.UUID) -> uuid.UUID:
    """Walk the account to PURGING and take a lifecycle claim on it."""
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'seam', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    return token


def _purge(cur, user: uuid.UUID, token: uuid.UUID) -> None:
    cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'seam')",
                (str(user), PHASE, str(token)))
    cur.execute("SELECT identity.purge_source_data(%s, %s, 'seam')",
                (str(user), str(token)))


def _remaining(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    return cur.fetchone()[0]


def _phase_completes(cur, user: uuid.UUID, token: uuid.UUID) -> bool:
    """The database's own verdict, not ours.

    `complete_lifecycle_phase` refuses while anything in scope remains, so this
    returning true is the purge being certified complete by the same function
    the privacy worker calls.
    """
    cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'seam')",
                (str(user), PHASE, str(token)))
    return bool(cur.fetchone()[0])


def _event_state(cur, event: uuid.UUID) -> tuple[str, int, str | None]:
    cur.execute("SELECT claim_state, attempts, last_error_code "
                "  FROM ioe.freshness_outbox WHERE id = %s", (str(event),))
    return cur.fetchone()


def _run_freshness(cur, user: uuid.UUID) -> str:
    cur.execute("SELECT freshness_status FROM ioe.optimization_run "
                " WHERE user_id = %s", (str(user),))
    return cur.fetchone()[0]


async def test_a_purge_between_apply_and_acknowledge_still_completes():
    """Race B. The tenant apply has already committed when the account's data
    is purged; the acknowledgement arrives afterwards, describing work done
    against rows that no longer exist.

    The dangerous outcome would be a purge that can never be certified because
    the late acknowledgement — or the apply's own writes — left something in
    scope behind. It does not: the apply writes only to sealed history, which
    the purge deliberately retains.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user, analysis = _account(cur)
        event = _queue_event(cur, user, analysis)
        token = _claim_token(cur, user)

        assert _run_freshness(cur, user) == "current", "nothing for the apply to do"
        assert _remaining(cur, user) > 0, "nothing for the purge to do"

        purged: list[bool] = []
        relay = FreshnessRelay("seam-b")
        original = relay._acknowledge

        async def purge_then_acknowledge(claimed, error_code):
            # Only at the seam of OUR event: another test's event passing
            # through this relay must not drag an unrelated account into a
            # purge.
            if claimed.event_id == event and not purged:
                _purge(cur, user, token)
                purged.append(True)
            return await original(claimed, error_code)

        relay._acknowledge = purge_then_acknowledge  # type: ignore[method-assign]
        await relay.drain(batch_size=200)

        assert purged, "the purge never ran at the seam; this ordering was not tested"

        state, _, _ = _event_state(cur, event)
        assert state == "completed", (
            f"the acknowledgement after a purge left the event {state}")
        assert _run_freshness(cur, user) == "stale", (
            "the apply did not take effect, so the interleaving is untested")
        assert _remaining(cur, user) == 0, "a purged source row came back"
        assert _phase_completes(cur, user, token), (
            "the purge could not be certified complete after a relay "
            "acknowledgement landed on top of it")
    finally:
        admin.close()


async def test_a_purge_before_the_claim_leaves_the_relay_nothing_to_resurrect():
    """Race C. The whole relay cycle runs against an account already purged.

    The apply step is the one to watch. It runs under
    `unit_of_work(user_id=event.user_id)` — ordinary RLS, no elevation — so
    after a purge it sees an emptied tenant and marks what is left of it.
    Sealed history is still there and is still staled; source data stays gone.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user, analysis = _account(cur)
        event = _queue_event(cur, user, analysis)
        token = _claim_token(cur, user)

        _purge(cur, user, token)
        assert _remaining(cur, user) == 0, "the purge did not run"
        assert _event_state(cur, event)[0] == "pending", (
            "the purge consumed the event, so the relay has nothing to race")

        await FreshnessRelay("seam-c").drain(batch_size=200)

        state, _, _ = _event_state(cur, event)
        assert state == "completed", (
            f"an obsolete event was not processed cleanly: {state}")
        assert _remaining(cur, user) == 0, "the relay resurrected source data"
        assert _phase_completes(cur, user, token), (
            "a full relay cycle after the purge blocked phase completion")
    finally:
        admin.close()


async def test_the_failure_branch_after_a_purge_records_a_code_not_a_message():
    """Race D. The apply raises, so the relay takes `fail_freshness_event`.

    Two things are at stake and both are privacy properties. Below the ceiling
    the event returns to `pending` rather than terminating, which is the
    behaviour the convergence proof depends on; and what gets stored is the
    enumerated code, never the exception. Entry 11A established that a
    SQLAlchemy exception string can carry financial values, and this is exactly
    the path that would write one down.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user, analysis = _account(cur)
        event = _queue_event(cur, user, analysis)
        token = _claim_token(cur, user)

        raised: list[bool] = []
        relay = FreshnessRelay("seam-d")
        original = relay._apply_for_tenant

        async def purge_then_fail(claimed):
            if claimed.event_id != event:
                return await original(claimed)
            if not raised:
                _purge(cur, user, token)
            raised.append(True)
            # A message shaped like the ones that must never be persisted.
            raise RuntimeError(f"apply failed for amount 4321 user {user}")

        relay._apply_for_tenant = purge_then_fail  # type: ignore[method-assign]
        # ONE pass, not a drain: `drain` re-claims a pending event on every
        # pass, which would run the failure five times and terminate it. The
        # property under test is the BELOW-ceiling branch.
        report = await relay.run_once(batch_size=200)

        assert raised, "the apply never failed; the failure branch was not tested"
        assert report.failed >= 1, "the relay did not record a failure"

        state, attempts, error_code = _event_state(cur, event)
        assert attempts < 5, "the fixture drove the event to the ceiling itself"
        assert state == "pending", (
            f"below the attempt ceiling a failed event must return to the "
            f"queue, not sit in {state}")
        assert error_code == ERROR_TENANT_APPLY_FAILED, (
            f"expected the enumerated code, stored {error_code!r}")

        cur.execute("SELECT to_jsonb(o)::text FROM ioe.freshness_outbox o "
                    " WHERE id = %s", (str(event),))
        row = cur.fetchone()[0]
        assert "4321" not in row and "apply failed" not in row, (
            "the exception text reached the queue row")
        cur.execute("SELECT coalesce(string_agg(to_jsonb(a)::text, ' '), '') "
                    "  FROM ioe.freshness_outbox_audit a WHERE event_id = %s",
                    (str(event),))
        assert "4321" not in cur.fetchone()[0], (
            "the exception text reached the outbox audit trail")

        assert _remaining(cur, user) == 0, "the failure path resurrected data"
        assert _phase_completes(cur, user, token), (
            "a failed relay attempt blocked phase completion")
    finally:
        admin.close()


async def test_a_claim_abandoned_across_a_purge_is_recovered_and_terminates():
    """The crash case: a worker claims an event, the account is purged, and the
    worker never comes back.

    The lease is what unsticks it — `claim_freshness_events` returns claims
    older than `ioe.freshness_claim_timeout()` to `pending` before selecting.
    Recovery must still happen when the tenant behind the event is gone, and
    the redelivered event must still be bounded, or a crash during a deletion
    would leave a queue row no operator action clears.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user, analysis = _account(cur)
        event = _queue_event(cur, user, analysis)
        token = _claim_token(cur, user)

        cur.execute("SELECT count(*) FROM ioe.claim_freshness_events(200, 'ghost')")
        assert _event_state(cur, event)[0] == "claimed", "the fixture claimed nothing"

        _purge(cur, user, token)

        # Age the claim past the lease instead of waiting it out. The timeout is
        # read from the function, so a change to it cannot make this test
        # quietly stop reaching the recovery branch.
        cur.execute("UPDATE ioe.freshness_outbox "
                    "   SET claimed_at = now() - ioe.freshness_claim_timeout() "
                    "                  - interval '1 minute' "
                    " WHERE id = %s", (str(event),))

        cur.execute("SELECT count(*) FROM ioe.claim_freshness_events(200, 'seam-e')")
        state, attempts, _ = _event_state(cur, event)
        assert state == "claimed" and attempts == 2, (
            f"the abandoned claim was not recovered and redelivered: "
            f"{state}, {attempts} attempts")
        cur.execute("SELECT count(*) FROM ioe.freshness_outbox_audit "
                    " WHERE event_id = %s AND transition = 'claim_recovered'",
                    (str(event),))
        assert cur.fetchone()[0] == 1, "recovery was not recorded"

        # And it is still bounded: drive the redelivered event to the ceiling.
        for _ in range(6):
            cur.execute(
                "SELECT count(*) FROM ioe.claim_freshness_events(200, 'seam-e')")
            cur.execute("SELECT ioe.fail_freshness_event(%s, 'seam-e', %s)",
                        (str(event), ERROR_TENANT_APPLY_FAILED))
        assert _event_state(cur, event)[0] == "failed", (
            "a recovered post-purge event is not bounded")

        assert _remaining(cur, user) == 0, "recovery resurrected source data"
        assert _phase_completes(cur, user, token), (
            "an abandoned claim across a purge blocked phase completion")
    finally:
        admin.close()
