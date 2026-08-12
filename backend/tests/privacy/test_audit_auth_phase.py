"""AUDIT_AUTH_DEIDENTIFICATION as a real lifecycle phase (Entry 11B6D).

The mechanism has existed since 11B6: `identity.deidentify_audit_auth` severs
the account from its retained audit and authentication history, and
`identity.count_attributable_audit_auth` says whether anything attributable is
left. What did not exist was anything that RAN them. The privacy worker knew
one phase, and an account whose SOURCE_DATA had finished sat in PURGING for
ever with its login history still naming a person.

Two things are proven here. That the worker now drives the phase through the
same claim, lease and capability machinery as SOURCE_DATA — no second
orchestrator. And that the phase cannot be recorded COMPLETE while attribution
survives, which before migration 0061 it could: `complete_lifecycle_phase`
enforced its "nothing in scope remains" rule for SOURCE_DATA only, and returned
true for this phase with five attributable rows still present.

WHAT COMPLETION DOES NOT MEAN. Audit history is not deleted. Security history
is not deleted. Historical actor ids are not rewritten, and the append-only
audit log is never written to at all. What is severed is every mapping that
resolves retained history back to a live account.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest

from app.database.models import UserAccount
from app.database.session import unit_of_work
from tests.conftest import owner_dsn

PHASE = "AUDIT_AUTH_DEIDENTIFICATION"
SOURCE = "SOURCE_DATA"


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


async def _account(tag: str = "11b6d") -> tuple[uuid.UUID, str]:
    async with unit_of_work(user_id=None, actor_type="system") as s:
        account = UserAccount(
            email=f"{tag}-{uuid.uuid4().hex[:10]}@example.test", status="active"
        )
        s.add(account)
        await s.flush()
        return account.id, account.email


def _identity_history(cur, uid, email, *, ip4="203.0.113.44",
                      ip6="2001:db8:1234:5678::1") -> None:
    """Auth and audit history that names a person, through the real tables."""
    cur.execute("INSERT INTO identity.account_subject (user_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING", (str(uid),))
    cur.execute("INSERT INTO identity.login_event "
                "(user_id, email_tried, event_type, ip_address, user_agent) "
                "VALUES (%s,%s,'success',%s,'probe-agent')", (str(uid), email, ip4))
    # The anonymous case: a failed login naming the person only by the email
    # somebody typed, with no user_id at all.
    cur.execute("INSERT INTO identity.login_event "
                "(user_id, email_tried, event_type, ip_address) "
                "VALUES (NULL,%s,'failure',%s)", (email, ip6))
    cur.execute("INSERT INTO audit.security_event (user_id, event_type, severity, "
                "ip_address) VALUES (%s,'probe','info','198.51.100.7')", (str(uid),))
    cur.execute("INSERT INTO audit.consent_log (user_id, consent_type, granted, "
                "ip_address) VALUES (%s,'terms',true,'198.51.100.8')", (str(uid),))


def _subject_key(cur, uid):
    cur.execute("SELECT subject_key FROM identity.account_subject WHERE user_id=%s",
                (str(uid),))
    row = cur.fetchone()
    return row[0] if row else None


def _attributable(cur, uid) -> int:
    cur.execute("SELECT identity.count_attributable_audit_auth(%s)", (str(uid),))
    return cur.fetchone()[0]


def _walk_to_purging(cur, uid) -> None:
    """Request deletion and walk the bookkeeping states, as the request path does."""
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s,'DELETION_REQUESTED')", (str(uid),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state=%s WHERE user_id=%s",
                    (state, str(uid)))


def _mark_source_data_complete(cur, uid) -> None:
    """Record the phases that run BEFORE this one as done.

    SOURCE_DATA, DOCUMENTS and SCENARIO_RETENTION each have their own proofs
    elsewhere; this file is about what happens after them. The worker runs one
    phase per claim in a fixed order, so all of them have to be recorded or it
    would keep choosing an earlier one and never reach audit/auth.
    """
    for phase in (SOURCE, "DOCUMENTS", "SCENARIO_RETENTION"):
        cur.execute("""
            INSERT INTO identity.account_lifecycle_phase
                   (user_id, phase, status, attempts, started_at, completed_at)
            VALUES (%s, %s, 'COMPLETE', 1, now(), now())
            ON CONFLICT (user_id, phase) DO UPDATE
               SET status='COMPLETE', completed_at=now()
        """, (str(uid), phase))


def _phase_status(cur, uid, phase=PHASE):
    cur.execute("SELECT status FROM identity.account_lifecycle_phase"
                " WHERE user_id=%s AND phase=%s", (str(uid), phase))
    row = cur.fetchone()
    return row[0] if row else None


async def _run_worker(worker_id="privacy-worker") -> dict[str, int]:
    """The real Celery task body, through the certified worker runtime.

    Dispatched to a thread rather than awaited: `run_task` owns the event loop
    for a task invocation, which is the whole point of `workers.runtime`, and it
    cannot build one inside the loop pytest-asyncio is already running. A
    thread gives it the same clean slate a Celery worker process does — and
    exercises the real synchronous entry point rather than reaching past it into
    the coroutine, which would test a code path production never takes.
    """
    import asyncio

    from workers.tasks.privacy import run_account_deletion_phases

    return await asyncio.to_thread(run_account_deletion_phases, worker_id=worker_id)


# --------------------------------------------------------------- the phase ---

async def test_the_worker_runs_the_phase_and_attribution_reaches_zero():
    """§14/§15 — driven by the real worker, not by calling the keyhole."""
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    key = _subject_key(cur, uid)
    assert key is not None
    assert _attributable(cur, uid) > 0, "the fixture is not attributable to begin with"

    _walk_to_purging(cur, uid)
    _mark_source_data_complete(cur, uid)

    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE", (
        f"the phase did not complete (status={_phase_status(cur, uid)})"
    )
    assert _attributable(cur, uid) == 0
    cur.execute("SELECT count(*) FROM identity.account_subject WHERE user_id=%s",
                (str(uid),))
    assert cur.fetchone()[0] == 0, "the live subject mapping survived the phase"
    cur.execute("SELECT count(*) FROM identity.deletion_subject WHERE subject_key=%s",
                (str(key),))
    assert cur.fetchone()[0] == 1, "the retired key was not tombstoned"

    # §32: the account itself is still here, and deletion is not finished.
    cur.execute("SELECT count(*) FROM identity.user_account WHERE id=%s", (str(uid),))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id=%s",
                (str(uid),))
    assert cur.fetchone()[0] != "COMPLETE"
    conn.close()


async def test_the_phase_cannot_complete_while_attribution_survives():
    """§8 — the fail-closed guard migration 0061 added.

    Before 0061 this returned true with five attributable rows present, because
    `complete_lifecycle_phase` checked its remaining-count for SOURCE_DATA only.
    The phase is started and completion attempted WITHOUT running the keyhole,
    which is precisely the shape of a worker bug that skipped the work.
    """
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    _walk_to_purging(cur, uid)

    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by='probe',"
                " claim_token=%s, claimed_at=now() WHERE user_id=%s",
                (str(token), str(uid)))
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'probe')",
                (str(uid), PHASE, str(token)))

    remaining = _attributable(cur, uid)
    assert remaining > 0
    with pytest.raises(psycopg2.Error) as excinfo:
        cur.execute("SELECT identity.complete_lifecycle_phase(%s,%s,%s,'probe')",
                    (str(uid), PHASE, str(token)))
    assert "cannot complete" in str(excinfo.value), excinfo.value
    assert _phase_status(cur, uid) != "COMPLETE"
    conn.close()


async def test_running_the_phase_twice_changes_nothing_the_second_time():
    """§22 — idempotency, and without rebuilding a reverse map to detect it.

    The absence of `identity.account_subject` IS the durable record. A second
    run finds no mapping, returns zeros, and must not recreate one to notice.
    """
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    key = _subject_key(cur, uid)
    _walk_to_purging(cur, uid)
    _mark_source_data_complete(cur, uid)

    await _run_worker()
    cur.execute("SELECT count(*) FROM identity.login_event WHERE subject_key=%s",
                (str(key),))
    after_first = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM identity.deletion_subject")
    tombstones_first = cur.fetchone()[0]

    # Re-claim and run again.
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by=NULL,"
                " claim_token=NULL, claimed_at=NULL WHERE user_id=%s", (str(uid),))
    cur.execute("UPDATE identity.account_lifecycle_phase SET status='RUNNING',"
                " completed_at=NULL WHERE user_id=%s AND phase=%s", (str(uid), PHASE))
    await _run_worker()

    cur.execute("SELECT count(*) FROM identity.login_event WHERE subject_key=%s",
                (str(key),))
    assert cur.fetchone()[0] == after_first, "the second run touched more rows"
    cur.execute("SELECT count(*) FROM identity.deletion_subject")
    assert cur.fetchone()[0] == tombstones_first, "a duplicate subject key was minted"
    cur.execute("SELECT count(*) FROM identity.account_subject WHERE user_id=%s",
                (str(uid),))
    assert cur.fetchone()[0] == 0, "the reverse mapping was recreated"
    assert _phase_status(cur, uid) == "COMPLETE"
    conn.close()


# ------------------------------------------------------------- what is kept --

async def test_the_audit_log_is_not_written_to_at_all():
    """§16 — byte identity on the append-only log.

    De-identification works by removing what a subject key RESOLVES TO, never
    by editing the rows that carry it. So the whole audit log for this subject
    must be byte-identical afterwards, actor ids and payloads included.
    """
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    key = _subject_key(cur, uid)

    cur.execute("INSERT INTO audit.audit_log (actor_type, actor_id, action, "
                "entity_schema, entity_table, entity_id, subject_key) "
                "VALUES ('user',%s,'UPDATE','profile','user_profile',%s,%s)",
                (str(uid), str(uid), str(key)))
    cur.execute("INSERT INTO audit.audit_log (actor_type, actor_id, action, "
                "entity_schema, entity_table, entity_id, subject_key) "
                "VALUES ('admin',%s,'UPDATE','profile','user_profile',%s,%s)",
                (str(uuid.uuid4()), str(uid), str(key)))

    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM audit.audit_log t WHERE subject_key=%s", (str(key),))
    before = cur.fetchone()[0]

    _walk_to_purging(cur, uid)
    _mark_source_data_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM audit.audit_log t WHERE subject_key=%s", (str(key),))
    assert cur.fetchone()[0] == before, (
        "an audit row changed during de-identification; the log is append-only "
        "and this phase must never write to it"
    )
    conn.close()


async def test_the_operator_actor_survives_while_the_subject_is_severed():
    """§18 — staff attribution is not collateral damage.

    An `operator X -> customer A` row is security evidence about X. Severing A
    must leave X exactly as it was: who acted is a different question from whom
    they acted upon, and the audit log keeps both in different columns.
    """
    uid, email = await _account()
    operator = uuid.uuid4()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    key = _subject_key(cur, uid)
    cur.execute("INSERT INTO audit.audit_log (actor_type, actor_id, action, "
                "entity_schema, entity_table, entity_id, subject_key) "
                "VALUES ('admin',%s,'UPDATE','profile','user_profile',%s,%s)",
                (str(operator), str(uid), str(key)))

    _walk_to_purging(cur, uid)
    _mark_source_data_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT actor_type, actor_id FROM audit.audit_log "
                " WHERE subject_key=%s AND actor_type='admin'", (str(key),))
    rows = cur.fetchall()
    assert rows, "the operator's audit row disappeared"
    assert all(str(r[1]) == str(operator) for r in rows), (
        "the operator's identity was de-identified along with the customer's"
    )
    # And the subject key it points at no longer resolves to anybody.
    cur.execute("SELECT count(*) FROM identity.account_subject WHERE subject_key=%s",
                (str(key),))
    assert cur.fetchone()[0] == 0
    conn.close()


async def test_login_history_keeps_the_event_and_loses_the_person():
    """§19/§20 — measured field by field, against the real coarsening code."""
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    key = _subject_key(cur, uid)
    _walk_to_purging(cur, uid)
    _mark_source_data_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT event_type, email_tried, user_agent, user_id, "
                "       host(ip_address), deidentified_at IS NOT NULL "
                "  FROM identity.login_event WHERE subject_key=%s ORDER BY 5",
                (str(key),))
    rows = cur.fetchall()
    assert len(rows) == 2, f"expected both login events on the key, got {len(rows)}"
    for event_type, email_tried, agent, user_id, _ip, deidentified in rows:
        assert event_type in ("success", "failure"), "the event itself was lost"
        assert email_tried is None, "the attempted email survived"
        assert agent is None, "the user agent survived"
        assert user_id is None, "the account link survived"
        assert deidentified, "the row is not marked de-identified"
    addresses = sorted(row[4] for row in rows)
    assert addresses == ["2001:db8:1234::", "203.0.113.0"], (
        f"IP coarsening did not produce the /24 and /48 networks: {addresses}"
    )
    conn.close()


async def test_an_unrelated_anonymous_login_is_not_touched():
    """§21 — the email match must not reach beyond this subject.

    The keyhole deliberately catches anonymous failures that name the person by
    the email typed. That is only correct if it catches THIS person's: a failed
    login for somebody else's address is unrelated security history.
    """
    uid, email = await _account()
    _, other_email = await _account("stranger")
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    cur.execute("INSERT INTO identity.login_event (user_id, email_tried, event_type,"
                " ip_address) VALUES (NULL,%s,'failure','198.51.100.99') RETURNING id",
                (other_email,))
    stranger_event = cur.fetchone()[0]

    _walk_to_purging(cur, uid)
    _mark_source_data_complete(cur, uid)
    await _run_worker()

    cur.execute("SELECT email_tried, host(ip_address), deidentified_at "
                "  FROM identity.login_event WHERE id=%s", (str(stranger_event),))
    email_tried, ip, deidentified = cur.fetchone()
    assert email_tried == other_email, "an unrelated anonymous login was de-identified"
    assert ip == "198.51.100.99", "an unrelated login's address was coarsened"
    assert deidentified is None
    conn.close()


# ---------------------------------------------------------------- recovery ---

async def test_a_crash_before_the_mutation_leaves_the_phase_incomplete():
    """§23 — claimed, then nothing. Retry must find work still owed."""
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    _walk_to_purging(cur, uid)

    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by='crasher',"
                " claim_token=%s, claimed_at=now() WHERE user_id=%s",
                (str(token), str(uid)))
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'crasher')",
                (str(uid), PHASE, str(token)))
    # ... and the worker dies here, before calling the keyhole.

    assert _phase_status(cur, uid) == "RUNNING"
    assert _attributable(cur, uid) > 0, "identity was lost without the keyhole running"

    _mark_source_data_complete(cur, uid)
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by=NULL,"
                " claim_token=NULL, claimed_at=NULL WHERE user_id=%s", (str(uid),))
    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE", "the retry did not converge"
    assert _attributable(cur, uid) == 0
    conn.close()


async def test_a_crash_after_the_mutation_still_converges_to_complete():
    """§24 — the exactly-once proof, and the interesting half.

    The keyhole succeeded and the worker died before recording completion. The
    retry must recognise already-severed state, NOT recreate a mapping in order
    to notice, and NOT mint a second subject key.
    """
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    key = _subject_key(cur, uid)
    _walk_to_purging(cur, uid)

    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by='crasher',"
                " claim_token=%s, claimed_at=now() WHERE user_id=%s",
                (str(token), str(uid)))
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'crasher')",
                (str(uid), PHASE, str(token)))
    cur.execute("SELECT identity.deidentify_audit_auth(%s,%s,'crasher')",
                (str(uid), str(token)))
    # ... crash here: the work is done, the phase row still says RUNNING.
    assert _phase_status(cur, uid) == "RUNNING"
    assert _attributable(cur, uid) == 0

    cur.execute("SELECT count(*) FROM identity.deletion_subject")
    tombstones = cur.fetchone()[0]

    _mark_source_data_complete(cur, uid)
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by=NULL,"
                " claim_token=NULL, claimed_at=NULL WHERE user_id=%s", (str(uid),))
    await _run_worker()

    assert _phase_status(cur, uid) == "COMPLETE"
    cur.execute("SELECT count(*) FROM identity.deletion_subject")
    assert cur.fetchone()[0] == tombstones, "the retry minted a second subject key"
    cur.execute("SELECT count(*) FROM identity.account_subject WHERE user_id=%s",
                (str(uid),))
    assert cur.fetchone()[0] == 0, "the retry recreated the reverse mapping"
    cur.execute("SELECT count(*) FROM identity.login_event WHERE subject_key=%s",
                (str(key),))
    assert cur.fetchone()[0] == 2, "the retry changed the retained history"
    conn.close()


async def test_a_stale_claim_token_cannot_run_or_complete_the_phase():
    """§25 — worker A's lease lapsed and B legitimately holds the subject."""
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    _walk_to_purging(cur, uid)

    stale = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by='worker-a',"
                " claim_token=%s, claimed_at=now() WHERE user_id=%s",
                (str(stale), str(uid)))
    fresh = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by='worker-b',"
                " claim_token=%s, claimed_at=now() WHERE user_id=%s",
                (str(fresh), str(uid)))

    with pytest.raises(psycopg2.Error) as excinfo:
        cur.execute("SELECT identity.deidentify_audit_auth(%s,%s,'worker-a')",
                    (str(uid), str(stale)))
    assert "no live claim" in str(excinfo.value), excinfo.value
    assert _attributable(cur, uid) > 0, "the stale worker mutated anyway"

    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,'worker-b')",
                (str(uid), PHASE, str(fresh)))
    cur.execute("SELECT identity.deidentify_audit_auth(%s,%s,'worker-b')",
                (str(uid), str(fresh)))
    assert _attributable(cur, uid) == 0
    cur.execute("SELECT identity.complete_lifecycle_phase(%s,%s,%s,'worker-b')",
                (str(uid), PHASE, str(fresh)))
    assert cur.fetchone()[0] is True

    # The stale token must not be able to record anything either.
    cur.execute("SELECT identity.fail_lifecycle_phase(%s,%s,%s,'STALE_PROBE','worker-a')",
                (str(uid), PHASE, str(stale)))
    assert cur.fetchone()[0] is False, "a stale token moved the phase"
    conn.close()


async def test_the_mapping_cannot_be_resurrected_after_the_phase():
    """§27 — zero attribution has to stay zero.

    `identity.account_subject` has no INSERT grant for any application role, so
    the only writer is the keyhole — and the keyhole recreates nothing. Proven
    from the runtime login rather than from the grant table.
    """
    uid, email = await _account()
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, uid, email)
    _walk_to_purging(cur, uid)
    _mark_source_data_complete(cur, uid)
    await _run_worker()
    assert _attributable(cur, uid) == 0

    # Two writers, refused by two different mechanisms — and both matter.
    # The mapping table is closed by PRIVILEGE: no application role holds
    # anything on it. The severed login row is closed by an INVARIANT, because
    # the application legitimately holds UPDATE on `identity.login_event` and
    # always will. That second one was open until this slice: writing a deleted
    # account id back onto a severed row made the account attributable again
    # after its phase was already COMPLETE.
    app = psycopg2.connect(owner_dsn().replace("onyx_migrator", "onyx_test"))
    app.autocommit = False
    try:
        acur = app.cursor()
        for statement, expected in (
            ("INSERT INTO identity.account_subject (user_id) VALUES (%s)",
             "permission denied"),
            ("UPDATE identity.login_event SET user_id=%s WHERE user_id IS NULL",
             "de-identified and cannot be modified"),
        ):
            with pytest.raises(psycopg2.Error) as excinfo:
                acur.execute(statement, (str(uid),))
            assert expected in str(excinfo.value).lower(), (
                f"expected {expected!r}, got: {excinfo.value}"
            )
            app.rollback()
    finally:
        app.close()

    assert _attributable(cur, uid) == 0
    conn.close()


# ------------------------------------------------------------ isolation -----

async def test_deidentifying_one_account_leaves_another_untouched():
    """§28/§29 — no broad email, hash or subject sweep."""
    a_uid, a_email = await _account("tenant-a")
    b_uid, b_email = await _account("tenant-b")
    conn = _owner()
    cur = conn.cursor()
    _identity_history(cur, a_uid, a_email)
    _identity_history(cur, b_uid, b_email, ip4="192.0.2.55", ip6="2001:db8:9999::9")
    b_key = _subject_key(cur, b_uid)
    b_before = _attributable(cur, b_uid)
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM identity.login_event t WHERE user_id=%s OR email_tried=%s",
                (str(b_uid), b_email))
    b_login_before = cur.fetchone()[0]

    _walk_to_purging(cur, a_uid)
    _mark_source_data_complete(cur, a_uid)
    await _run_worker()

    assert _attributable(cur, a_uid) == 0
    assert _attributable(cur, b_uid) == b_before, "B lost attribution material"
    assert _subject_key(cur, b_uid) == b_key, "B's subject key changed"
    cur.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) "
                "FROM identity.login_event t WHERE user_id=%s OR email_tried=%s",
                (str(b_uid), b_email))
    assert cur.fetchone()[0] == b_login_before, "B's login history changed"

    a_key = None
    cur.execute("SELECT subject_key FROM identity.login_event WHERE email_tried IS NULL"
                " AND subject_key IS NOT NULL AND user_id IS NULL LIMIT 1")
    row = cur.fetchone()
    if row:
        a_key = row[0]
    assert a_key != b_key, "A and B share a subject key"
    conn.close()


# ------------------------------------------------------------- boundaries ----

def test_only_the_privacy_worker_may_run_the_keyhole():
    """§11 — PD-16, proven from genuine LOGINs rather than from the ACL."""
    probe = uuid.uuid4()
    for login, allowed in (("onyx_test", False), ("onyx_privacy_test", True)):
        conn = psycopg2.connect(owner_dsn().replace("onyx_migrator", login))
        conn.autocommit = False
        try:
            cur = conn.cursor()
            cur.execute("SELECT current_database(), session_user, "
                        "current_setting('is_superuser')")
            database, principal, superuser = cur.fetchone()
            assert superuser == "off", f"{principal} is a superuser in {database}"

            try:
                cur.execute("SELECT identity.deidentify_audit_auth(%s,%s,'probe')",
                            (str(probe), str(uuid.uuid4())))
                refused = None
            except psycopg2.Error as exc:
                refused = str(exc)
            if allowed:
                # It may fail on the claim check — that is the function running.
                assert refused is None or "no live claim" in refused, (
                    f"the privacy worker cannot execute its own keyhole: {refused}"
                )
            else:
                assert refused and "permission denied" in refused.lower(), (
                    f"{principal} was able to execute the de-identification keyhole"
                )
        finally:
            conn.rollback()
            conn.close()


def test_the_privacy_worker_gained_no_broader_powers():
    """§11/§33 — the capability stayed a keyhole, and 0060's guard stands."""
    conn = _owner()
    try:
        cur = conn.cursor()
        cur.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                    " WHERE rolname='onyx_privacy_worker'")
        superuser, bypassrls, canlogin = cur.fetchone()
        assert not superuser and not bypassrls and not canlogin, (
            "onyx_privacy_worker is no longer a NOLOGIN capability role"
        )
        for role in ("onyx_privacy_worker", "onyx_app_rw"):
            cur.execute("SELECT has_table_privilege(%s,'identity.user_account','DELETE')",
                        (role,))
            assert cur.fetchone()[0] is False, (
                f"{role} gained DELETE on identity.user_account; 0060 withdrew it "
                "and terminal removal is not this slice's to enable"
            )
        # The mapping table is the one no role may reach: it is what resolves a
        # retained subject key back to an account. `identity.login_event` is
        # deliberately NOT in this list — the application writes login events
        # during authentication and always has. What matters is that the
        # DE-IDENTIFYING update runs inside the keyhole, which the privacy
        # worker having no direct UPDATE is what proves.
        for role in ("onyx_privacy_worker", "onyx_app_rw", "onyx_app_ro"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cur.execute("SELECT has_table_privilege(%s,'identity.account_subject',%s)",
                            (role, privilege))
                assert cur.fetchone()[0] is False, (
                    f"{role} gained {privilege} on identity.account_subject; the "
                    "mapping must be reachable only through the keyhole"
                )
        cur.execute("SELECT has_table_privilege('onyx_privacy_worker',"
                    "'identity.login_event','UPDATE')")
        assert cur.fetchone()[0] is False, (
            "the privacy worker gained direct UPDATE on identity.login_event; "
            "de-identification must stay inside the SECURITY DEFINER keyhole"
        )
    finally:
        conn.close()


def test_terminal_deletion_is_still_blocked_by_the_registry():
    """§32 — finishing this phase does not bring terminal deletion closer."""
    from tests.privacy.account_delete_registry import (
        assert_terminal_account_delete_ready,
        terminal_delete_blockers,
    )

    with pytest.raises(AssertionError):
        assert_terminal_account_delete_ready()
    assert len(terminal_delete_blockers()) == 27
