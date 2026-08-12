"""HISTORICAL_DETAIL_CLEANUP — the phase the other four left behind (Entry 11B6I).

Fifteen tables of derived per-person tax detail survived all four certified
phases. Since 0060 detached the sealed roots from `identity.user_account` they
would outlive the account carrying a `user_id` that resolves to nobody.

WHY NONE OF THEM IS `DERIVED_DELETE`. A draft of this entry argued that eight
were reproducible because their values are committed into
`optimization_result_hash` / `scenario_result_hash`. That is a category error: a
digest commits to a preimage, it does not permit reconstructing one. Neither
root stores a structured result payload, so the only reconstruction route is
re-executing the engine over retained inputs — and
`test_recomputation_is_not_a_durable_property` measures that route failing the
moment the running build stops matching the pins. All fifteen are therefore
`LIVE_USER_DATA_DELETE`, on the rationale that does not depend on
reconstructability: nothing reads them once the account is gone.

TWO INDEPENDENT AUTHORITIES, NEVER ONE. `EXPECTED_PURGE_SET` comes from the
canonical privacy registry; `IMPLEMENTED_PURGE_SET` is parsed out of the SQL
keyhole. If the test derived both from the SQL, a table omitted from the keyhole
would vanish from the implementation and from its own coverage at the same time.
The invariant is equality in BOTH directions:

    EXPECTED - IMPLEMENTED  → a table nothing deletes
    IMPLEMENTED - EXPECTED  → a table nothing authorised

and the verification set comes from the 11B6H module that measured it, not from
a copy.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import psycopg2
import pytest

from app.database.session import unit_of_work
from tests.conftest import owner_dsn
from tests.privacy.account_delete_registry import (
    CLASSIFIED,
    DELETING_CLASSIFICATIONS,
    REGISTRY,
    RETAINING_CLASSIFICATIONS,
    UNCLASSIFIED_BLOCKING,
)

# The 11B6H authority for what integrity verification reads. Imported, never
# duplicated: a second copy is a second thing to forget to update.
from tests.privacy.test_verification_consumers import VERIFICATION_READS, _sealed_chain

PHASE = "HISTORICAL_DETAIL_CLEANUP"
EARLIER_PHASES = ("SOURCE_DATA", "DOCUMENTS", "SCENARIO_RETENTION")

_SQL = Path(__file__).resolve().parents[2] / "db" / "sql" / "58_historical_detail_cleanup.sql"

#: The engineering surfaces this entry set out to resolve. Recorded explicitly
#: because once they are classified they stop being blockers, and "the fifteen"
#: has to keep meaning something afterwards.
RESOLVED_BY_11B6I = frozenset({
    "ioe.candidate_cost", "ioe.candidate_economic_effect",
    "ioe.confidence_component", "ioe.multi_year_projection",
    "ioe.optimization_run_event", "ioe.portfolio_evaluation_step",
    "ioe.recommendation_relationship", "ioe.scenario_confidence_component",
    "ioe.scenario_event", "ioe.scenario_input_change", "ioe.scenario_result",
    "ioe.score_component", "analysis.analysis_assumption",
    "analysis.analysis_line_item", "analysis.reconciliation_check",
})

#: How to reach one account's rows in each purged table. Mirrors the keyhole's
#: own WHERE clauses; used only to take the keyhole apart one group at a time.
_SCOPE = {
    "ioe.candidate_cost":
        "candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c "
        "JOIN ioe.optimization_run r ON r.id=c.run_id WHERE r.user_id=%(u)s)",
    "ioe.candidate_economic_effect":
        "candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c "
        "JOIN ioe.optimization_run r ON r.id=c.run_id WHERE r.user_id=%(u)s)",
    "ioe.confidence_component":
        "candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c "
        "JOIN ioe.optimization_run r ON r.id=c.run_id WHERE r.user_id=%(u)s)",
    "ioe.score_component":
        "candidate_id IN (SELECT c.id FROM ioe.optimization_candidate c "
        "JOIN ioe.optimization_run r ON r.id=c.run_id WHERE r.user_id=%(u)s)",
    "ioe.recommendation_relationship":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
    "ioe.multi_year_projection":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
    "ioe.optimization_run_event":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
    "ioe.portfolio_evaluation_step":
        "portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p "
        "JOIN ioe.optimization_run r ON r.id=p.run_id WHERE r.user_id=%(u)s)",
    "ioe.scenario_result":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%(u)s)",
    "ioe.scenario_input_change":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%(u)s)",
    "ioe.scenario_confidence_component":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%(u)s)",
    "ioe.scenario_event":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%(u)s)",
    "analysis.analysis_line_item":
        "analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id=%(u)s)",
    "analysis.analysis_assumption":
        "analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id=%(u)s)",
    "analysis.reconciliation_check":
        "analysis_id IN (SELECT id FROM analysis.analysis_run WHERE user_id=%(u)s)",
}


# --------------------------------------------------------------- authorities --
def implemented_purge_set() -> set[str]:
    """Every table the keyhole actually deletes, read out of the SQL."""
    body = _SQL.read_text()
    return set(re.findall(r"DELETE\s+FROM\s+([a-z0-9_]+\.[a-z0-9_]+)", body))


def expected_purge_set() -> set[str]:
    """Every table the REGISTRY says account deletion removes, among the 15."""
    return {
        t for t in RESOLVED_BY_11B6I
        if REGISTRY[t].state == CLASSIFIED
        and REGISTRY[t].classification in DELETING_CLASSIFICATIONS
    }


def retain_set() -> set[str]:
    return {
        t for t in RESOLVED_BY_11B6I
        if REGISTRY[t].state == CLASSIFIED
        and REGISTRY[t].classification in RETAINING_CLASSIFICATIONS
    }


def still_unresolved() -> set[str]:
    return {t for t in RESOLVED_BY_11B6I if REGISTRY[t].state == UNCLASSIFIED_BLOCKING}


# -------------------------------------------------------------------- helpers --
def _owner():
    c = psycopg2.connect(owner_dsn())
    c.autocommit = True
    return c


def _walk_to_purging(cur, uid) -> None:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s,'DELETION_REQUESTED') ON CONFLICT (user_id) DO NOTHING",
                (str(uid),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state=%s WHERE user_id=%s",
                    (state, str(uid)))


def _mark_earlier_complete(cur, uid) -> None:
    for phase in EARLIER_PHASES:
        cur.execute("""
            INSERT INTO identity.account_lifecycle_phase
                   (user_id, phase, status, attempts, started_at, completed_at)
            VALUES (%s, %s, 'COMPLETE', 1, now(), now())
            ON CONFLICT (user_id, phase) DO UPDATE
               SET status='COMPLETE', completed_at=now()
        """, (str(uid), phase))


def _claim(cur, uid, worker="probe") -> uuid.UUID:
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by=%s,"
                " claim_token=%s, claimed_at=now() WHERE user_id=%s",
                (worker, str(token), str(uid)))
    return token


def _remaining(cur, uid) -> int:
    cur.execute("SELECT identity.count_remaining_historical_detail(%s)", (str(uid),))
    return cur.fetchone()[0]


def _phase_status(cur, uid, phase=PHASE):
    cur.execute("SELECT status FROM identity.account_lifecycle_phase"
                " WHERE user_id=%s AND phase=%s", (str(uid), phase))
    row = cur.fetchone()
    return row[0] if row else None


def _purge(cur, uid, token) -> None:
    cur.execute("SELECT * FROM identity.purge_historical_detail(%s,%s,%s)",
                (str(uid), str(token), "probe"))


def _complete(cur, uid, token) -> bool:
    cur.execute("SELECT identity.complete_lifecycle_phase(%s,%s,%s,%s)",
                (str(uid), PHASE, str(token), "probe"))
    return cur.fetchone()[0]


def _start(cur, uid, token) -> None:
    cur.execute("SELECT identity.start_lifecycle_phase(%s,%s,%s,%s)",
                (str(uid), PHASE, str(token), "probe"))


async def _prepared_account():
    """A production-sealed chain whose account is claimed and mid-PURGING."""
    uid, run_id, portfolio_id, scenario_id = await _sealed_chain()
    c = _owner()
    cur = c.cursor()
    _walk_to_purging(cur, uid)
    _mark_earlier_complete(cur, uid)
    token = _claim(cur, uid)
    c.close()
    return uid, run_id, portfolio_id, scenario_id, token


# ------------------------------------------------------------- set invariants --
def test_the_starting_fifteen_partition_into_exactly_one_disposition_each():
    """No table may vanish from the accounting because the SQL omitted it."""
    purge, retain, unresolved = expected_purge_set(), retain_set(), still_unresolved()
    assert purge | retain | unresolved == set(RESOLVED_BY_11B6I)
    assert purge & retain == set()
    assert purge & unresolved == set()
    assert retain & unresolved == set()
    assert len(RESOLVED_BY_11B6I) == 15


def test_expected_and_implemented_purge_sets_agree():
    """Registry and keyhole are independent authorities and must match.

    Derived from different artifacts on purpose: the registry is hand-argued
    classification, the implementation is parsed out of the SQL. Equality in one
    direction only would let an omitted table hide.
    """
    expected, implemented = expected_purge_set(), implemented_purge_set()
    missing = sorted(expected - implemented)
    unauthorised = sorted(implemented - expected)
    assert missing == [], (
        f"the registry says account deletion removes these, and nothing does: {missing}")
    assert unauthorised == [], (
        f"the keyhole deletes these and no registry entry authorises it: {unauthorised}")


def test_the_purge_set_and_the_verification_set_are_disjoint():
    """The invariant the whole phase is built around.

    `VERIFICATION_READS` is the 11B6H measurement. If the keyhole ever reaches
    one of those tables, a verified artifact becomes a mismatch — the outcome
    indistinguishable from tampering.
    """
    overlap = sorted(implemented_purge_set() & set(VERIFICATION_READS))
    assert overlap == [], f"the cleanup would destroy verification evidence: {overlap}"


def test_every_purged_table_is_declared_deletable_in_the_classification_registry():
    from app.privacy.classification import LIFECYCLE, DeletionAction

    offenders = sorted(
        f"{t}: {LIFECYCLE[t].on_account_deletion.value}"
        for t in implemented_purge_set()
        if LIFECYCLE[t].on_account_deletion is DeletionAction.RETAIN
    )
    assert offenders == [], f"declared RETAIN but purged: {offenders}"


def test_no_purged_table_claims_a_replay_dependency():
    from app.privacy.classification import LIFECYCLE

    offenders = sorted(t for t in implemented_purge_set()
                       if LIFECYCLE[t].replay_dependency)
    assert offenders == [], f"replay depends on these and the phase deletes them: {offenders}"


# ------------------------------------------------------------ the phase itself --
async def test_the_phase_removes_every_group_and_completes():
    uid, _run, _pf, _sc, token = await _prepared_account()
    c = _owner()
    cur = c.cursor()
    try:
        before = _remaining(cur, uid)
        assert before > 0, "fixture produced no detail — the case proves nothing"
        _start(cur, uid, token)
        _purge(cur, uid, token)
        assert _remaining(cur, uid) == 0
        assert _complete(cur, uid, token) is True
        assert _phase_status(cur, uid) == "COMPLETE"
    finally:
        c.close()


def test_the_completion_guard_counts_every_table_the_keyhole_deletes():
    """Guard-on-the-guard, structurally: no group may be missing from the count.

    A branch absent from `count_remaining_historical_detail` would let the phase
    report COMPLETE with rows still present — the exact failure the guard exists
    to prevent. Checked against the SQL rather than against a fixture, so it
    covers groups a fixture cannot populate (`analysis.analysis_assumption` has
    no production writer at all).
    """
    body = _SQL.read_text()
    guard = body[body.index("count_remaining_historical_detail"):body.index("purge_historical_detail")]
    missing = sorted(t for t in implemented_purge_set() if t not in guard)
    assert missing == [], f"the keyhole deletes these and the guard never counts them: {missing}"


async def test_deleting_each_group_moves_the_completion_guard():
    """Guard-on-the-guard, dynamically: each group's rows are really counted.

    Removes one table at a time and requires the guard to fall by exactly the
    number of rows removed. A table the guard forgot would leave the number
    unchanged while rows disappeared.
    """
    uid, run_id, portfolio_id, scenario_id, token = await _prepared_account()
    c = _owner()
    cur = c.cursor()
    try:
        _start(cur, uid, token)
        exercised = []
        for table in sorted(implemented_purge_set()):
            before = _remaining(cur, uid)
            cur.execute("SET app.allow_evidence_purge = 'on'")
            cur.execute(f"DELETE FROM {table} WHERE {_SCOPE[table]}", {"u": str(uid)})
            removed = cur.rowcount
            after = _remaining(cur, uid)
            assert before - after == removed, (
                f"{table}: {removed} rows removed but the guard moved "
                f"{before - after}"
            )
            if removed:
                exercised.append(table)
            # while anything at all remains, completion must refuse
            if after:
                with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
                    _complete(cur, uid, token)
                assert "cannot complete" in str(excinfo.value)
        assert _remaining(cur, uid) == 0
        assert len(exercised) >= 10, (
            f"only {len(exercised)} groups were populated; the dynamic half of "
            f"the guard-on-the-guard proved too little: {exercised}"
        )
        assert _complete(cur, uid, token) is True
    finally:
        c.close()


async def test_running_the_phase_twice_changes_nothing_the_second_time():
    uid, _r, _p, _s, token = await _prepared_account()
    c = _owner()
    cur = c.cursor()
    try:
        _start(cur, uid, token)
        _purge(cur, uid, token)
        assert _complete(cur, uid, token) is True
        _purge(cur, uid, token)          # idempotent: nothing left to remove
        assert _remaining(cur, uid) == 0
        assert _phase_status(cur, uid) == "COMPLETE"
        cur.execute("SELECT count(*) FROM identity.account_lifecycle_phase"
                    " WHERE user_id=%s AND phase=%s", (str(uid), PHASE))
        assert cur.fetchone()[0] == 1, "a second lifecycle artifact was created"
    finally:
        c.close()


async def test_a_stale_claim_can_neither_purge_nor_complete():
    uid, _r, _p, _s, token = await _prepared_account()
    c = _owner()
    cur = c.cursor()
    try:
        stale = token
        _claim(cur, uid, worker="the-worker-that-took-over")   # reclaim
        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            _purge(cur, uid, stale)
        assert "claim token does not hold this subject" in str(excinfo.value)
        assert _complete(cur, uid, stale) is False
        assert _remaining(cur, uid) > 0, "a refused purge must not have removed rows"
    finally:
        c.close()


async def test_cross_tenant_detail_is_untouched():
    a_uid, _r, _p, _s, token = await _prepared_account()
    b_uid, b_run, b_pf, b_scenario = await _sealed_chain()
    c = _owner()
    cur = c.cursor()
    try:
        before = _remaining(cur, b_uid)
        assert before > 0
        cur.execute("SELECT md5(string_agg(t::text, '|' ORDER BY t::text))"
                    "  FROM ioe.scenario_result t"
                    " WHERE scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%s)",
                    (str(b_uid),))
        b_digest = cur.fetchone()[0]

        _start(cur, a_uid, token)
        _purge(cur, a_uid, token)
        assert _remaining(cur, a_uid) == 0

        assert _remaining(cur, b_uid) == before, "B lost rows to A's purge"
        cur.execute("SELECT md5(string_agg(t::text, '|' ORDER BY t::text))"
                    "  FROM ioe.scenario_result t"
                    " WHERE scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%s)",
                    (str(b_uid),))
        assert cur.fetchone()[0] == b_digest, "B's rows are not byte-identical"
    finally:
        c.close()


async def test_replay_and_integrity_still_verify_after_the_cleanup():
    """The whole point. The detail goes; the evidence keeps verifying."""
    from app.services.ioe.domain.integrity import EntityType
    from app.services.ioe.replay.verification import IntegrityVerificationService

    uid, run_id, portfolio_id, scenario_id, token = await _prepared_account()
    svc = IntegrityVerificationService(uid)

    async def verify():
        out = {}
        for label, kind, eid in (("OPTIMIZATION", EntityType.OPTIMIZATION, run_id),
                                 ("PORTFOLIO", EntityType.PORTFOLIO, portfolio_id),
                                 ("SCENARIO", EntityType.SCENARIO, scenario_id)):
            r = await svc.verify(kind, eid)
            out[label] = f"{r.status}/{r.reason_code}"
        return out

    before = await verify()
    assert before == {"OPTIMIZATION": "verified/NONE", "PORTFOLIO": "verified/NONE",
                      "SCENARIO": "verified/NONE"}

    c = _owner()
    cur = c.cursor()
    try:
        _start(cur, uid, token)
        _purge(cur, uid, token)
        assert _remaining(cur, uid) == 0
        # every verification-required table still populated
        for table, pred in (
            ("ioe.optimization_candidate",
             "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%s)"),
            ("ioe.run_rule_version",
             "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%s)"),
            ("ioe.scenario_lever",
             "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%s)"),
            ("ioe.scenario_assumption",
             "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%s)"),
        ):
            cur.execute(f"SELECT count(*) FROM {table} WHERE {pred}", (str(uid),))
            assert cur.fetchone()[0] > 0, f"{table} was destroyed by the cleanup"
    finally:
        c.close()

    assert await verify() == before, "the cleanup changed a verification outcome"


async def test_recomputation_is_not_a_durable_property():
    """Why none of the fifteen is DERIVED_DELETE.

    Only a digest of the detail survives, so the sole reconstruction route is
    re-running the engine over retained inputs. That route is gated on the
    running build matching the pins, and it closes the moment it does not.
    """
    from app.services.ioe.domain.integrity import EntityType, IntegrityReason
    from app.services.ioe.replay import resolver as res_mod
    from app.services.ioe.replay.verification import IntegrityVerificationService

    uid, run_id, portfolio_id, scenario_id = await _sealed_chain()
    svc = IntegrityVerificationService(uid)
    first = await svc.verify(EntityType.OPTIMIZATION, run_id)
    assert str(first.status) == "verified"

    original = dict(res_mod.EXECUTABLE_VERSIONS)
    res_mod.EXECUTABLE_VERSIONS["tax_engine_version"] = (
        "99.0.0", IntegrityReason.PINNED_ENGINE_VERSION_UNAVAILABLE)
    try:
        after = await svc.verify(EntityType.OPTIMIZATION, run_id)
    finally:
        res_mod.EXECUTABLE_VERSIONS.clear()
        res_mod.EXECUTABLE_VERSIONS.update(original)

    assert str(after.status) == "unavailable"
    assert "PINNED_ENGINE_VERSION_UNAVAILABLE" in str(after.reason_code)


def _app_dsn() -> str:
    """A GENUINE application LOGIN, never the owner with SET ROLE.

    Derived from the URL the harness provisioned, so it names whatever database
    this run created rather than a hardcoded one.
    """
    import os
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(os.environ["ONYX_DATABASE_URL"])
    database = (parsed.path or "/onyx_test").lstrip("/").split("?")[0]
    query = parse_qs(parsed.query)
    return (
        f"dbname={database} user={parsed.username} password={parsed.password} "
        f"host={query.get('host', ['/var/run/postgresql'])[0]} "
        f"port={query.get('port', ['5432'])[0]}"
    )


def test_the_guc_is_not_the_authorisation_boundary():
    """`SET app.allow_evidence_purge = 'on'` must grant the app role nothing.

    This is the hole 11B6I found and closed. `onyx_app_rw` held DELETE on all
    twenty-three sealed-detail tables, and `ioe.reject_result_mutation` was the
    only refusal — which any role can switch off, because a GUC is a request,
    not a privilege. Two statements from an ordinary session could destroy the
    tenant's own sealed replay evidence.

    Asserted from a real LOGIN and by capability, never by reading the
    migration and never by connecting as the owner and using SET ROLE.
    """
    connection = psycopg2.connect(_app_dsn())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, session_user, current_database()")
            current_user, session_user, database = cursor.fetchone()
            assert current_user == session_user, "this must be a genuine login"
            cursor.execute(
                "SELECT pg_has_role(current_user, 'onyx_app_rw', 'USAGE')")
            assert cursor.fetchone()[0] is True, "the login must carry the app role"
            # Never assert against a hardcoded database name.
            assert database

            # The `ioe` sealed tables only: the three `analysis` ones carry no
            # insert-only trigger, so the GUC was never their boundary and PD-1
            # tenant CRUD is their intended model.
            protected = sorted(
                t for t in (implemented_purge_set() | set(VERIFICATION_READS))
                if t.startswith("ioe.")
            )
            for table in protected:
                cursor.execute(
                    "SELECT has_table_privilege(current_user, %s, 'DELETE')", (table,))
                assert cursor.fetchone()[0] is False, (
                    f"{table}: the application role can DELETE sealed detail, so "
                    f"the trigger GUC is the only barrier and is not a boundary")

            # And the statement itself is refused even with the GUC set.
            cursor.execute("BEGIN")
            cursor.execute("SET LOCAL app.allow_evidence_purge = 'on'")
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cursor.execute("DELETE FROM ioe.scenario_result WHERE true")
            cursor.execute("ROLLBACK")
    finally:
        connection.close()


def test_the_privacy_worker_reaches_the_detail_only_through_the_keyhole():
    """No broad DELETE grant stands behind the phase."""
    connection = psycopg2.connect(owner_dsn())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for table in sorted(implemented_purge_set()):
                cursor.execute(
                    "SELECT has_table_privilege('onyx_privacy_worker', %s, 'DELETE')",
                    (table,))
                assert cursor.fetchone()[0] is False, (
                    f"{table}: the privacy worker holds DELETE directly, so "
                    f"'erase one account' is also 'erase everyone'")
            cursor.execute(
                "SELECT has_function_privilege('onyx_privacy_worker',"
                " 'identity.purge_historical_detail(uuid,uuid,text)', 'EXECUTE')")
            assert cursor.fetchone()[0] is True
            cursor.execute(
                "SELECT has_function_privilege('public',"
                " 'identity.purge_historical_detail(uuid,uuid,text)', 'EXECUTE')")
            assert cursor.fetchone()[0] is False, "PUBLIC can execute the keyhole"
    finally:
        connection.close()


async def test_a_crash_between_groups_converges_on_retry():
    """Crash before, mid and after the mutation all converge.

    The keyhole is one statement per group inside one transaction, so a crash
    before it leaves everything and a crash during it leaves nothing
    half-removed. What has to be shown is that a crash AFTER the purge but
    BEFORE completion does not strand the account: the retry finds zero
    outstanding and completes.
    """
    uid, _run, _pf, _sc, token = await _prepared_account()
    c = _owner()
    cur = c.cursor()
    try:
        # crash before any mutation: nothing removed, nothing completed
        _start(cur, uid, token)
        assert _remaining(cur, uid) > 0
        assert _phase_status(cur, uid) != "COMPLETE"

        # crash mid-purge: the transaction rolls back as a unit
        cur.execute("BEGIN")
        cur.execute("SELECT * FROM identity.purge_historical_detail(%s,%s,%s)",
                    (str(uid), str(token), "probe"))
        cur.execute("ROLLBACK")
        assert _remaining(cur, uid) > 0, "a rolled-back purge removed rows"

        # crash after the purge, before COMPLETE
        _purge(cur, uid, token)
        assert _remaining(cur, uid) == 0
        assert _phase_status(cur, uid) != "COMPLETE"

        # the retry converges
        _purge(cur, uid, token)
        assert _remaining(cur, uid) == 0
        assert _complete(cur, uid, token) is True
    finally:
        c.close()


async def test_no_product_read_path_reaches_another_accounts_detail():
    """Post-account-removal reachability, proven through RLS.

    The three tables with a live reader are reached only by account-scoped
    routes behind `Depends(current_user_id)`. Once the account is gone there is
    no such caller, and a same-email successor is a DIFFERENT user_id — which is
    what this asserts, because "the account is gone" is not a claim a test can
    make about an HTTP dependency, while "RLS excludes these rows" is.
    """
    from app.services.ioe.read_repository import IoeReadRepository
    from app.services.ioe.scenario.query_service import ScenarioQueryService

    a_uid, a_run, _pf, a_scenario = await _sealed_chain()

    # A same-email successor: a new account, therefore a new user_id.
    from app.database.models import UserAccount
    async with unit_of_work(user_id=None, actor_type="system") as s:
        successor = UserAccount(
            email=f"successor-{uuid.uuid4().hex[:10]}@example.test", status="active")
        s.add(successor)
        await s.flush()
        b_uid = successor.id
    assert b_uid != a_uid

    async with unit_of_work(user_id=b_uid, actor_type="user") as s:
        repo = IoeReadRepository(s, b_uid)
        assert list(await repo.projections_for_run(a_run)) == []
        assert await repo.scenario_result(a_scenario) is None
        assert list(await repo.scenario_changes(a_scenario)) == []

    # And the detail route refuses outright rather than leaking a row. The
    # response is identical to "no such scenario", so the successor cannot even
    # learn that the predecessor's scenario existed.
    from app.core.exceptions import NotFound

    with pytest.raises(NotFound):
        await ScenarioQueryService(b_uid).detail(a_scenario)
