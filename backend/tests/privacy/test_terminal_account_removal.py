"""Terminal account removal, and the Actor Attribution Decision Gate (11B6J).

Every entry since 11B6C forbade `identity.terminal_remove_account` by name.
This is the entry that writes it, and the reason it is safe to write now is
that the five lifecycle phases and their completion guards already exist: the
keyhole does not decide anything, it refuses unless the database can show that
everything else already finished.

PRODUCTION ENABLEMENT IS OFF. `test_no_worker_calls_the_terminal_keyhole`
asserts that, and it is not a formality — a scheduled worker that physically
removed people because a queue reached them is precisely what this entry
declines to build.

THE PD-15 GATE. `audit.audit_log.actor_id` holds a deleted customer's own UUID,
the table is append-only, and `trg_audit_immutable` refuses UPDATE even for the
owner. Nothing here rewrites it — making a privacy number look better by
editing immutable audit history would be the worst available outcome. Instead
`test_the_actor_attribution_inventory_after_terminal_removal` MEASURES what
that surviving UUID can still reach, which is the only honest way to decide
whether it is an orphaned pseudonymous correlator or a live attribution path.
"""
from __future__ import annotations

import psycopg2
import pytest

from tests.conftest import owner_dsn
from tests.privacy.test_historical_detail_cleanup import (
    _claim,
    _owner,
    _phase_status,
    _walk_to_purging,
)
from tests.privacy.test_verification_consumers import _sealed_chain

PHASES = (
    "SOURCE_DATA",
    "DOCUMENTS",
    "SCENARIO_RETENTION",
    "HISTORICAL_DETAIL_CLEANUP",
    "AUDIT_AUTH_DEIDENTIFICATION",
)


def _mark_all_phases_complete(cur, uid) -> None:
    for phase in PHASES:
        cur.execute("""
            INSERT INTO identity.account_lifecycle_phase
                   (user_id, phase, status, attempts, started_at, completed_at)
            VALUES (%s, %s, 'COMPLETE', 1, now(), now())
            ON CONFLICT (user_id, phase) DO UPDATE
               SET status='COMPLETE', completed_at=now()
        """, (str(uid), phase))


def _terminal_remove(cur, uid, token, worker="probe"):
    cur.execute("SELECT identity.terminal_remove_account(%s,%s,%s)",
                (str(uid), str(token), worker))
    return cur.fetchone()[0]


def _account_exists(cur, uid) -> bool:
    cur.execute("SELECT count(*) FROM identity.user_account WHERE id=%s", (str(uid),))
    return cur.fetchone()[0] == 1


async def _run_worker(worker_id="privacy-worker") -> dict[str, int]:
    """The real Celery task body, through the certified worker runtime."""
    import asyncio

    from workers.tasks.privacy import run_account_deletion_phases

    return await asyncio.to_thread(run_account_deletion_phases, worker_id=worker_id)


async def _fully_purged_account():
    """A sealed account walked to the edge of terminal removal BY THE WORKER.

    An earlier version of this fixture marked the five phase rows COMPLETE
    without running them, and the keyhole refused — correctly, naming
    `SOURCE_DATA: 3 rows` and `AUDIT_AUTH_DEIDENTIFICATION: 1 rows`. That is the
    guard doing its job, and the fixture was the thing that was lying. So the
    phases are driven for real: one per worker invocation, until all five report
    COMPLETE.
    """
    uid, run_id, portfolio_id, scenario_id = await _sealed_chain()

    # DISPOSE BEFORE HANDING OFF TO THE WORKER. `_sealed_chain` drives the
    # production services on pytest's event loop, leaving pooled connections
    # bound to it; `run_task` then owns a DIFFERENT loop inside its thread, and
    # a pooled connection crossing that boundary dies with "attached to a
    # different loop" — a message that reads like a database fault and is not
    # one. The directory conftest disposes BETWEEN tests; this hand-off happens
    # inside one.
    from app.database.privacy_session import dispose_all_engines

    await dispose_all_engines()

    c = _owner()
    cur = c.cursor()
    _walk_to_purging(cur, uid)

    # ONE PHASE PER CLAIM, and the claim holds a ten-minute lease — so a tight
    # loop of worker invocations claims nothing after the first. Releasing the
    # lease between runs is what expiry does in production, and without it only
    # SOURCE_DATA ever completed here.
    for _ in range(12):
        if all(_phase_status(cur, uid, phase) == "COMPLETE" for phase in PHASES):
            break
        await _run_worker()
        cur.execute("UPDATE identity.account_lifecycle"
                    "   SET claimed_by=NULL, claim_token=NULL, claimed_at=NULL"
                    " WHERE user_id=%s", (str(uid),))
    else:
        missing = [p for p in PHASES if _phase_status(cur, uid, p) != "COMPLETE"]
        raise AssertionError(f"the worker did not finish these phases: {missing}")

    token = _claim(cur, uid)
    return uid, run_id, portfolio_id, scenario_id, token, c, cur


# ------------------------------------------------------------ enablement --
def test_no_worker_calls_the_terminal_keyhole():
    """Production enablement is OFF, asserted against the source.

    The dispatcher runs five phases and stops. If a future change wires the
    keyhole in, this fails and the decision becomes explicit instead of
    arriving inside an unrelated diff.
    """
    from pathlib import Path

    worker = Path(__file__).resolve().parents[2] / "workers" / "tasks" / "privacy.py"
    body = worker.read_text()
    assert "terminal_remove_account" not in body, (
        "the privacy worker now calls terminal removal — enablement was OFF by "
        "decision, so this needs an explicit entry, not a quiet wiring")

    service = (Path(__file__).resolve().parents[2] / "app" / "services" / "privacy"
               / "lifecycle.py")
    assert "terminal_remove_account" not in service.read_text(), (
        "the lifecycle service now calls terminal removal")


def test_the_keyhole_is_reachable_only_through_the_privacy_worker():
    connection = psycopg2.connect(owner_dsn())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            signature = "identity.terminal_remove_account(uuid,uuid,text)"
            cursor.execute(
                "SELECT has_function_privilege('onyx_privacy_worker', %s, 'EXECUTE')",
                (signature,))
            assert cursor.fetchone()[0] is True
            cursor.execute(
                "SELECT has_function_privilege('public', %s, 'EXECUTE')", (signature,))
            assert cursor.fetchone()[0] is False, "PUBLIC can execute terminal removal"
            cursor.execute(
                "SELECT has_function_privilege('onyx_app_rw', %s, 'EXECUTE')",
                (signature,))
            assert cursor.fetchone()[0] is False, "the app role can execute it"
            # And no role gained a broad DELETE as a shortcut.
            for role in ("onyx_app_rw", "onyx_app_ro", "onyx_privacy_worker"):
                cursor.execute(
                    "SELECT has_table_privilege(%s,'identity.user_account','DELETE')",
                    (role,))
                assert cursor.fetchone()[0] is False, (
                    f"{role} holds DELETE on identity.user_account directly")
    finally:
        connection.close()


# ----------------------------------------------------------- fail-closed --
async def test_removal_is_refused_while_any_phase_is_incomplete():
    """Guard-on-the-guard, one phase at a time.

    Each of the five is knocked back to PENDING on its own and removal must
    refuse naming that phase. A guard that only checked the first would pass a
    single-phase test and lose four accounts' worth of data.
    """
    uid, _r, _p, _s, token, c, cur = await _fully_purged_account()
    try:
        for phase in PHASES:
            cur.execute("UPDATE identity.account_lifecycle_phase"
                        "   SET status='PENDING', completed_at=NULL"
                        " WHERE user_id=%s AND phase=%s", (str(uid), phase))
            with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
                _terminal_remove(cur, uid, token)
            assert phase in str(excinfo.value), (
                f"removal refused, but not because of {phase}: {excinfo.value}")
            assert _account_exists(cur, uid), "a refused removal deleted the account"
            cur.execute("UPDATE identity.account_lifecycle_phase"
                        "   SET status='COMPLETE', completed_at=now()"
                        " WHERE user_id=%s AND phase=%s", (str(uid), phase))
    finally:
        c.close()


async def test_removal_is_refused_while_a_completion_guard_is_non_zero():
    """A phase row is a claim; the counts are the evidence.

    All five phases say COMPLETE here. One row is put back so a guard reads
    non-zero, and removal must still refuse — proving the keyhole re-reads the
    evidence rather than trusting the claim.
    """
    uid, run_id, _p, _s, token, c, cur = await _fully_purged_account()
    try:
        assert _account_exists(cur, uid)
        # A source-data row, not an ioe detail row: the 11B6I write cutoff
        # refuses the latter for a deleting account, which is itself correct and
        # would make this test prove the wrong refusal.
        cur.execute("SELECT id FROM ref.income_type LIMIT 1")
        income_type_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO finance.income_source"
            "   (user_id, tax_year, income_type_id, amount, province_code)"
            " VALUES (%s, 2025, %s, 1234.00, 'ON')", (str(uid), income_type_id))

        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            _terminal_remove(cur, uid, token)
        message = str(excinfo.value)
        assert "privacy work remains" in message
        assert "SOURCE_DATA" in message
        assert _account_exists(cur, uid), "a refused removal deleted the account"
    finally:
        c.close()


async def test_a_stale_claim_cannot_remove_an_account():
    uid, _r, _p, _s, token, c, cur = await _fully_purged_account()
    try:
        stale = token
        _claim(cur, uid, worker="the-worker-that-took-over")
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            _terminal_remove(cur, uid, stale)
        assert "claim token does not hold this subject" in str(excinfo.value)
        assert _account_exists(cur, uid)
    finally:
        c.close()


async def test_removal_is_refused_outside_purging():
    uid, _r, _p, _s, token, c, cur = await _fully_purged_account()
    try:
        cur.execute("UPDATE identity.account_lifecycle SET state='FAILED_RETRYABLE'"
                    " WHERE user_id=%s", (str(uid),))
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            _terminal_remove(cur, uid, token)
        assert "not PURGING" in str(excinfo.value)
        assert _account_exists(cur, uid)
    finally:
        c.close()


# --------------------------------------------------------------- removal --
async def test_terminal_removal_completes_the_lifecycle_and_keeps_the_evidence():
    from app.services.ioe.domain.integrity import EntityType
    from app.services.ioe.replay.verification import IntegrityVerificationService

    uid, run_id, portfolio_id, scenario_id, token, c, cur = (
        await _fully_purged_account())
    try:
        assert _account_exists(cur, uid)
        assert _terminal_remove(cur, uid, token) is True
        assert not _account_exists(cur, uid), "the account survived its own removal"

        cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id=%s",
                    (str(uid),))
        assert cur.fetchone()[0] == "COMPLETE"

        # PD-9: the durable record that a deletion happened outlives the account.
        cur.execute("SELECT count(*) FROM identity.account_lifecycle_phase"
                    " WHERE user_id=%s", (str(uid),))
        assert cur.fetchone()[0] == len(PHASES)

        # The four sealed roots survive, because 0060 detached them.
        for table in ("analysis.analysis_run", "ioe.optimization_run", "ioe.scenario"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE user_id=%s", (str(uid),))
            assert cur.fetchone()[0] > 0, f"{table} did not outlive the account"

        # COMPLETE is terminal: a second call is refused outright, and the
        # transition trigger would refuse COMPLETE -> PURGING anyway.
        # A second call is refused at the first gate: completion released the
        # claim token, so the lease no longer holds the subject. Refusing there
        # rather than at the state check is if anything stricter.
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            _terminal_remove(cur, uid, token)
        assert "claim token does not hold this subject" in str(excinfo.value)
    finally:
        c.close()

    # And the sealed evidence still verifies with the person gone.
    service = IntegrityVerificationService(uid)
    for kind, entity_id in ((EntityType.OPTIMIZATION, run_id),
                            (EntityType.PORTFOLIO, portfolio_id),
                            (EntityType.SCENARIO, scenario_id)):
        result = await service.verify(kind, entity_id)
        assert str(result.status) == "verified", (
            f"{kind} stopped verifying after terminal removal: "
            f"{result.status}/{result.reason_code}")


async def test_removing_one_account_leaves_another_untouched():
    a_uid, _ar, _ap, _as_, a_token, c, cur = await _fully_purged_account()
    b_uid, b_run, _bp, _bs = await _sealed_chain()
    try:
        cur.execute("SELECT count(*) FROM ioe.optimization_run WHERE user_id=%s",
                    (str(b_uid),))
        b_runs_before = cur.fetchone()[0]
        assert b_runs_before > 0

        assert _terminal_remove(cur, a_uid, a_token) is True

        assert _account_exists(cur, b_uid), "B was removed with A"
        cur.execute("SELECT count(*) FROM ioe.optimization_run WHERE user_id=%s",
                    (str(b_uid),))
        assert cur.fetchone()[0] == b_runs_before, "B lost sealed history to A"
    finally:
        c.close()


# ------------------------------------- PD-15 actor attribution inventory --
#: Every column in the database that could hold a person's account UUID. Built
#: from the catalogue rather than a hand list, so a table added later cannot
#: quietly become a new attribution path.
_UUID_COLUMN_SQL = """
    SELECT n.nspname, c.relname, a.attname
      FROM pg_attribute a
      JOIN pg_class c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_type t ON t.oid = a.atttypid
     WHERE c.relkind = 'r'
       AND a.attnum > 0 AND NOT a.attisdropped
       AND t.typname = 'uuid'
       AND n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND (a.attname LIKE '%user_id%' OR a.attname LIKE '%actor%'
            OR a.attname = 'id')
"""


async def test_the_actor_attribution_inventory_after_terminal_removal():
    """THE ACTOR ATTRIBUTION DECISION GATE — measured, not argued.

    After A is physically removed and a same-email successor B registers, this
    walks every uuid column in the database that could hold a person's account
    id and records where A's old UUID still appears. Then it asks the only
    question that decides PD-15: can that UUID still reach a prohibited
    personal-identity path — email, profile, credentials, subject identity, or
    B?

    Nothing here rewrites audit history. The point is to find out what the
    surviving correlator is worth, not to make it disappear.
    """
    from app.database.models import UserAccount
    from app.database.session import unit_of_work

    a_uid, _r, _p, _s, token, c, cur = await _fully_purged_account()
    try:
        cur.execute("SELECT email FROM identity.user_account WHERE id=%s",
                    (str(a_uid),))
        a_email = cur.fetchone()[0]
        assert _terminal_remove(cur, a_uid, token) is True

        # Same-email re-registration: B is a NEW person as far as the database
        # is concerned, and must not inherit anything of A's.
        async with unit_of_work(user_id=None, actor_type="system") as session:
            successor = UserAccount(email=a_email, status="active")
            session.add(successor)
            await session.flush()
            b_uid = successor.id
        assert b_uid != a_uid, "the successor reused the removed account's id"

        # ---- inventory -------------------------------------------------------
        cur.execute(_UUID_COLUMN_SQL)
        columns = cur.fetchall()
        assert len(columns) > 20, "the catalogue scan found implausibly little"

        holders: dict[str, int] = {}
        for schema, table, column in columns:
            cur.execute(
                f'SELECT count(*) FROM "{schema}"."{table}" WHERE "{column}" = %s',
                (str(a_uid),))
            count = cur.fetchone()[0]
            if count:
                holders[f"{schema}.{table}.{column}"] = count

        # ---- prohibited paths, each asserted individually --------------------
        # 1. the account itself
        assert not _account_exists(cur, a_uid)
        # 2. email / profile / credentials
        for table in ("identity.user_account", "identity.user_credential",
                      "profile.user_profile", "profile.tax_profile"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE user_id=%s"
                        if table != "identity.user_account"
                        else f"SELECT count(*) FROM {table} WHERE id=%s",
                        (str(a_uid),))
            assert cur.fetchone()[0] == 0, f"{table} still resolves A's UUID"
        # 3. subject identity — the live mapping is severed, the tombstone
        #    carries no user_id at all
        cur.execute("SELECT count(*) FROM identity.account_subject WHERE user_id=%s",
                    (str(a_uid),))
        assert cur.fetchone()[0] == 0, "the live subject mapping survived removal"
        cur.execute("SELECT count(*) FROM information_schema.columns"
                    " WHERE table_schema='identity' AND table_name='deletion_subject'"
                    "   AND column_name='user_id'")
        assert cur.fetchone()[0] == 0, (
            "deletion_subject regained a user_id column — that is the join PD-15 "
            "was about")
        # 4. B
        assert not any(k.startswith("identity.user_account") for k in holders)
        cur.execute("SELECT count(*) FROM identity.user_account"
                    " WHERE id=%s AND id=%s", (str(b_uid), str(a_uid)))
        assert cur.fetchone()[0] == 0

        # ---- what DOES still hold it ----------------------------------------
        # Recorded rather than asserted empty: the surviving holders are the
        # evidence PD-15 is decided on. Every one of them must be a retained
        # artifact or immutable history, never a live identity mapping.
        # THE CATALOGUE SCAN EARNED ITS KEEP HERE. A hand-written list would
        # have missed both `audit.audit_log_default` — a PARTITION of
        # `audit_log`, so the same PD-15 residue under a different relname —
        # and `identity.account_lifecycle_event`, a transition log nobody had
        # enumerated. Neither is a live identity mapping, and both are named
        # explicitly rather than pattern-matched away.
        allowed_prefixes = (
            "audit.audit_log.actor_id",          # immutable history, PD-15 residue
            "audit.audit_log_default.actor_id",  # ...and its partition
            "analysis.analysis_run.user_id",     # retained sealed root (0060)
            "ioe.optimization_run.user_id",
            "ioe.scenario.user_id",
            "ioe.integrity_check.user_id",
            "identity.account_lifecycle.user_id",        # PD-9 deletion record
            "identity.account_lifecycle_phase.user_id",
            "identity.account_lifecycle_event.user_id",  # ...and its transitions
        )
        unexpected = sorted(
            k for k in holders if not k.startswith(allowed_prefixes))
        assert unexpected == [], (
            f"A's UUID survives somewhere unaccounted for: {unexpected}. Each "
            f"needs classifying before PD-15 can be decided.")

        print("\nPD-15 ATTRIBUTION INVENTORY after terminal removal:")
        for key in sorted(holders):
            print(f"    {key}: {holders[key]} row(s)")
    finally:
        c.close()


async def test_the_surviving_audit_actor_id_reaches_no_personal_identity():
    """The narrow question PD-15 turns on.

    `audit.audit_log.actor_id` keeps A's UUID and cannot be rewritten. This
    asserts what that UUID can and cannot be joined to once A is gone: it must
    reach no email, no profile, no credential and no subject key.
    """
    from app.database.models import UserAccount
    from app.database.session import unit_of_work

    a_uid, _r, _p, _s, token, c, cur = await _fully_purged_account()
    try:
        cur.execute("SELECT email FROM identity.user_account WHERE id=%s",
                    (str(a_uid),))
        a_email = cur.fetchone()[0]
        assert _terminal_remove(cur, a_uid, token) is True

        async with unit_of_work(user_id=None, actor_type="system") as session:
            successor = UserAccount(email=a_email, status="active")
            session.add(successor)
            await session.flush()
            b_uid = successor.id

        # The correlator survives — that is the honest starting point.
        cur.execute("SELECT count(*) FROM audit.audit_log WHERE actor_id=%s",
                    (str(a_uid),))
        surviving = cur.fetchone()[0]

        # ...and resolves to nothing that identifies a person.
        cur.execute("""
            SELECT count(*) FROM audit.audit_log al
              JOIN identity.user_account ua ON ua.id = al.actor_id
             WHERE al.actor_id = %s
        """, (str(a_uid),))
        assert cur.fetchone()[0] == 0, "actor_id still joins to a live account"

        cur.execute("""
            SELECT count(*) FROM audit.audit_log al
              JOIN identity.account_subject s ON s.user_id = al.actor_id
             WHERE al.actor_id = %s
        """, (str(a_uid),))
        assert cur.fetchone()[0] == 0, "actor_id still resolves to a subject key"

        # And it is not B's. Same email, different person.
        cur.execute("SELECT count(*) FROM audit.audit_log"
                    " WHERE actor_id=%s AND actor_id=%s", (str(a_uid), str(b_uid)))
        assert cur.fetchone()[0] == 0

        print(f"\nPD-15: {surviving} audit row(s) retain A's UUID; it resolves to "
              f"no account, no subject key, and is not B's")
    finally:
        c.close()


def test_audit_history_was_not_rewritten():
    """The line this entry will not cross to make a number look better."""
    connection = psycopg2.connect(owner_dsn())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c"
                " ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname='audit' AND c.relname='audit_log'"
                "   AND NOT t.tgisinternal AND t.tgname='trg_audit_immutable'")
            assert cursor.fetchone()[0] == 1, "audit_log lost its immutability trigger"

            # And the keyhole issues no statement against audit history. Only
            # executable lines are considered: the file discusses audit_log at
            # length in its comments, which is the opposite of touching it.
            from pathlib import Path

            sql = (Path(__file__).resolve().parents[2] / "db" / "sql"
                   / "60_terminal_account_removal.sql").read_text()
            executable = " ".join(
                line for line in sql.splitlines() if not line.strip().startswith("--")
            ).lower()
            for verb in ("update audit.", "delete from audit.", "insert into audit."):
                assert verb not in executable, (
                    f"terminal removal issues '{verb}' against audit history")
    finally:
        connection.close()


async def test_a_crash_after_removal_converges_on_retry():
    """The account is gone but the lifecycle never got its COMPLETE.

    A crash between a committed removal and whatever follows must converge, not
    wedge. An earlier draft of the keyhole returned false here and left the
    lifecycle in PURGING permanently — the person removed, the record still
    saying a purge was owed.
    """
    uid, _r, _p, _s, token, c, cur = await _fully_purged_account()
    try:
        # Simulate the crash: the account is gone, the lifecycle is untouched.
        cur.execute("DELETE FROM identity.user_account WHERE id=%s", (str(uid),))
        assert not _account_exists(cur, uid)
        cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id=%s",
                    (str(uid),))
        assert cur.fetchone()[0] == "PURGING"

        # The retry removes nothing — and says so — but converges the lifecycle.
        assert _terminal_remove(cur, uid, token) is False
        cur.execute("SELECT state, completed_at IS NOT NULL"
                    "  FROM identity.account_lifecycle WHERE user_id=%s", (str(uid),))
        state, has_completed_at = cur.fetchone()
        assert state == "COMPLETE", "a crashed removal left the lifecycle wedged"
        assert has_completed_at is True
    finally:
        c.close()
