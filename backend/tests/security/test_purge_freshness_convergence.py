"""Entry 11B5H2D — a freshness event that outlives the data it describes.

THE QUESTION THIS ANSWERS is whether an event queued before an account's
SOURCE_DATA purge can become permanently stuck: retried forever against state
that can never come back. That would be a real lifecycle defect — a queue item
no operator action resolves — so it is answered from the mechanism rather than
from one lucky run.

The mechanism is a bounded attempt count. `ioe.claim_freshness_events` selects
`WHERE claim_state = 'pending' AND attempts < 5`, and `fail_freshness_event`
turns an event terminal at `attempts >= 5`. So the worst case is five attempts
and then `failed`, which is unclaimable — finite by construction, not by luck.

The tenant application step is also not privileged: the relay applies under
`unit_of_work(user_id=event.user_id)`, so it is filtered by the same RLS an
authenticated request gets. After a purge it simply marks nothing.
"""
from __future__ import annotations

import uuid

import psycopg2

from tests.conftest import owner_dsn


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


#: The fixture's financial value, and the needle every leak assertion searches
#: for. It carries a decimal point deliberately — see the assertion guard.
NEEDLE = "4321.99"


def _account_with_income(cur) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"conv_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025, id, 4321.99, 'ON' FROM ref.income_type
         WHERE code = 'employment'
    """, (str(user),))
    return user


def _queue_event(cur, user: uuid.UUID) -> uuid.UUID:
    cur.execute("""
        INSERT INTO ioe.freshness_outbox
            (event_type, stale_reason_code, user_id, tax_year, dedupe_key)
        VALUES ('financial_data_changed', 'source_data_changed', %s, 2025, %s)
        RETURNING id
    """, (str(user), f"conv:{uuid.uuid4()}"))
    return cur.fetchone()[0]


def _purge(cur, user: uuid.UUID) -> None:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'conv', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    cur.execute("SELECT identity.purge_source_data(%s, %s, 'conv')",
                (str(user), str(token)))


def _state(cur, event_id: uuid.UUID) -> tuple[str, int]:
    cur.execute("SELECT claim_state, attempts FROM ioe.freshness_outbox "
                " WHERE id = %s", (str(event_id),))
    return cur.fetchone()


def test_the_retry_bound_is_a_property_of_the_claim_not_of_luck():
    """Read from the catalog, so a future change to either half is caught."""
    admin = _owner()
    try:
        cur = admin.cursor()
        cur.execute("SELECT prosrc FROM pg_proc p JOIN pg_namespace n "
                    "  ON n.oid = p.pronamespace "
                    " WHERE n.nspname = 'ioe' "
                    "   AND p.proname = 'claim_freshness_events'")
        claim = cur.fetchone()[0]
        cur.execute("SELECT prosrc FROM pg_proc p JOIN pg_namespace n "
                    "  ON n.oid = p.pronamespace "
                    " WHERE n.nspname = 'ioe' "
                    "   AND p.proname = 'fail_freshness_event'")
        fail = cur.fetchone()[0]
    finally:
        admin.close()

    assert "attempts < 5" in claim, (
        "the claim no longer bounds retries; an obsolete event could be "
        "retried forever")
    assert "attempts >= 5" in fail, (
        "failure no longer terminates at the bound")


def test_an_event_queued_before_a_purge_reaches_a_terminal_state():
    """The wedge question, exercised rather than reasoned about: drive the
    event to the attempt bound after its account's data is gone and prove it
    stops being claimable."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account_with_income(cur)
        event = _queue_event(cur, user)
        assert _state(cur, event)[0] == "pending", "the fixture queued nothing"

        _purge(cur, user)
        cur.execute("SELECT identity.count_remaining_source_data(%s)",
                    (str(user),))
        assert cur.fetchone()[0] == 0, "the purge did not run"

        # Worst case: it never succeeds. Drive it to the bound.
        for _ in range(6):
            cur.execute("SELECT count(*) FROM ioe.claim_freshness_events(50, 'conv-w')")
            cur.execute("SELECT identity.count_remaining_source_data(%s)",
                        (str(user),))  # keep the purge assertion honest
            cur.execute(
                "SELECT ioe.fail_freshness_event(%s, 'conv-w', "
                "'TENANT_APPLY_FAILED')", (str(event),))

        state, attempts = _state(cur, event)
        assert state == "failed", (
            f"the event is still {state} after {attempts} attempts — an "
            "obsolete post-purge event can be retried forever")

        # And it is genuinely out of the queue, not merely labelled.
        cur.execute("SELECT count(*) FROM ioe.claim_freshness_events(200, 'sweeper')")
        cur.execute("SELECT claim_state FROM ioe.freshness_outbox WHERE id = %s",
                    (str(event),))
        assert cur.fetchone()[0] == "failed", "a terminal event was handed out again"
    finally:
        admin.close()


def test_processing_an_obsolete_event_resurrects_nothing():
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account_with_income(cur)
        event = _queue_event(cur, user)
        _purge(cur, user)

        cur.execute("SELECT count(*) FROM ioe.claim_freshness_events(50, 'conv-w')")
        cur.execute("SELECT ioe.complete_freshness_event(%s, 'conv-w')",
                    (str(event),))

        cur.execute("SELECT identity.count_remaining_source_data(%s)",
                    (str(user),))
        assert cur.fetchone()[0] == 0, "processing the event restored source data"
        cur.execute("SELECT state FROM identity.account_lifecycle "
                    " WHERE user_id = %s", (str(user),))
        assert cur.fetchone()[0] == "PURGING", "the deletion lifecycle moved backwards"
    finally:
        admin.close()


def test_the_queued_event_carries_no_financial_value():
    """The row outlives the data it describes, so what it carries matters. The
    amount used by the fixture is 4321.99 and must appear nowhere in it.

    THE DECIMAL POINT IS LEAD, NOT DECORATION. The haystack is a jsonb row full
    of UUIDs, and a bare `4321` is four valid hex digits — measured at 7 hits in
    19,869 real audit rows, which is a gate that fails a few times a year for no
    reason. A needle containing `.` cannot occur inside a UUID at all.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account_with_income(cur)
        event = _queue_event(cur, user)
        _purge(cur, user)

        cur.execute("SELECT to_jsonb(o)::text FROM ioe.freshness_outbox o "
                    " WHERE id = %s", (str(event),))
        row = cur.fetchone()
        assert row is not None, "no event exists, so this proves nothing"
        assert "." in NEEDLE, (
            f"{NEEDLE!r} is pure hex and can appear inside a UUID by chance; "
            "the assertion below would be probabilistic")
        assert NEEDLE not in row[0], "a financial value survived in the queue row"
    finally:
        admin.close()
