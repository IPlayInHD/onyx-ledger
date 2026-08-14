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
from tests.privacy.test_verification_consumers import (
    VERIFICATION_READS,
    _sealed_chain,
)
from tests.privacy.test_verification_consumers import (
    _verification_reads_for_production as VERIFICATION_READS_FOR_PRODUCTION,
)

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


#: How to reach one account's rows in the tables integrity verification reads.
#: Used only to prove the cleanup leaves them exactly as it found them.
_VERIFICATION_SCOPE = {
    "ioe.optimization_candidate":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
    "ioe.run_rule_version":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
    "ioe.strategy_portfolio":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id=%(u)s)",
    "ioe.portfolio_member":
        "portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p "
        "JOIN ioe.optimization_run r ON r.id=p.run_id WHERE r.user_id=%(u)s)",
    "ioe.portfolio_exclusion":
        "portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p "
        "JOIN ioe.optimization_run r ON r.id=p.run_id WHERE r.user_id=%(u)s)",
    "ioe.resource_ledger_entry":
        "portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p "
        "JOIN ioe.optimization_run r ON r.id=p.run_id WHERE r.user_id=%(u)s)",
    "ioe.scenario_lever":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%(u)s)",
    "ioe.scenario_assumption":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id=%(u)s)",
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
    """The invariant the whole phase is built around, NARROWED — not relaxed.

    `VERIFICATION_READS` is the 11B6H measurement. If the keyhole reaches one of
    those tables, a verified artifact becomes a mismatch — indistinguishable
    from tampering. Keeping the sets disjoint was how that was guaranteed.

    Entry 12B1 Phase B produced one table where disjointness is impossible:
    `ioe.scenario_result` is read by v2 verification AND must keep being purged,
    because retaining derived tax results so a DELETED account's scenarios stay
    verifiable would put replay convenience above the deletion the user asked
    for.

    So the rule is now about the OUTCOME rather than the sets. An overlap is
    permitted only where the table has explicitly declared
    `post_account_deletion_replay`, and
    `test_an_authorized_purge_yields_unavailable_not_mismatch` proves the
    declaration true by executing the real workflow. Everything undeclared is
    refused exactly as before.
    """
    from app.privacy.classification import LIFECYCLE

    overlap = implemented_purge_set() & set(VERIFICATION_READS_FOR_PRODUCTION())
    undeclared = sorted(
        t for t in overlap
        if LIFECYCLE[t].post_account_deletion_replay is None
    )
    assert undeclared == [], (
        f"the cleanup would destroy verification evidence with no declared "
        f"lifecycle exception: {undeclared}")


def test_every_purged_table_is_declared_deletable_in_the_classification_registry():
    from app.privacy.classification import LIFECYCLE, DeletionAction

    offenders = sorted(
        f"{t}: {LIFECYCLE[t].on_account_deletion.value}"
        for t in implemented_purge_set()
        if LIFECYCLE[t].on_account_deletion is DeletionAction.RETAIN
    )
    assert offenders == [], f"declared RETAIN but purged: {offenders}"


def test_no_purged_table_claims_a_replay_dependency_without_declaring_why():
    """Same narrowing, from the classification side.

    A purged replay dependency must NAME itself as a governed lifecycle
    exception. Silence is still refused — the failure mode this guards is a
    table quietly acquiring a replay read and being purged anyway, which is how
    an authorized cleanup starts reporting as tampering.
    """
    from app.privacy.classification import LIFECYCLE

    offenders = sorted(
        t for t in implemented_purge_set()
        if LIFECYCLE[t].replay_dependency
        and LIFECYCLE[t].post_account_deletion_replay is None
    )
    assert offenders == [], (
        f"replay depends on these, the phase deletes them, and none declares "
        f"post_account_deletion_replay: {offenders}")


def test_the_lifecycle_exception_is_not_a_general_permission():
    """GUARD ON THE GUARD (§8).

    The exception must stay specific. Two things are asserted: exactly the
    tables we intend carry it today, and a hypothetical second replay
    dependency added to the purge set without declaring one is still refused by
    the invariant above — proved by running that invariant's own predicate over
    a mutated copy rather than by trusting the code path.
    """
    from app.privacy.classification import LIFECYCLE

    declared = sorted(
        t for t, e in LIFECYCLE.items()
        if e.post_account_deletion_replay is not None)
    assert declared == ["ioe.scenario_result"], (
        f"the lifecycle exception spread beyond the case it was approved for: "
        f"{declared}")

    # a replay dependency that is purged but undeclared must still be caught
    hypothetical_purged = implemented_purge_set() | {"ioe.scenario_lever"}
    offenders = sorted(
        t for t in hypothetical_purged
        if LIFECYCLE[t].replay_dependency
        and LIFECYCLE[t].post_account_deletion_replay is None
    )
    assert offenders == ["ioe.scenario_lever"], (
        "adding an undeclared replay dependency to the purge set was not "
        f"refused; the exception is too broad: {offenders}")


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

    # ENTRY 12B1 PHASE B CHANGED THIS OUTCOME, DELIBERATELY.
    #
    # Optimization and portfolio are untouched: their verification-required
    # tables survive the cleanup and they still verify, which is the guarantee
    # this test was written for and it is intact.
    #
    # The scenario is different now. A v2 seal binds a counterfactual derived
    # state living in `ioe.scenario_result`, and that table IS purged — Decision
    # B chose to keep deleting derived tax results rather than retain them so a
    # deleted account's scenarios stay replayable. So the honest post-cleanup
    # answer for a scenario is `unavailable`: the evidence is gone because the
    # user asked for it to be gone.
    #
    # What must NEVER happen is `mismatch`. That is the ambiguity the original
    # 11B6 invariant existed to prevent — an authorized cleanup reading as
    # tampering — and it is asserted explicitly below rather than implied.
    after = await verify()
    assert after["OPTIMIZATION"] == "verified/NONE", after
    assert after["PORTFOLIO"] == "verified/NONE", after
    assert after["SCENARIO"] == "unavailable/SEALED_EVIDENCE_INCOMPLETE", after
    assert "mismatch" not in after["SCENARIO"], (
        "an authorized lifecycle purge reported as tampering")


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


# ------------------------------------------------- readiness, census, gaps --
def test_account_removal_engineering_readiness_is_derived_not_declared():
    """ACCOUNT_REMOVAL_ENGINEERING_READY, and what it deliberately excludes.

    Engineering readiness means every engineering surface carries an
    evidence-backed disposition and every purgeable one has an implemented
    lifecycle treatment. It does NOT mean launch readiness, and the two are kept
    apart here so that a green engineering gate can never be mistaken for
    permission to ship.
    """
    from tests.privacy.account_delete_registry import terminal_delete_blockers

    blockers = set(terminal_delete_blockers())
    engineering = {t for t in blockers if not t.startswith("billing.")}
    assert engineering == set(), (
        f"engineering surfaces are still unresolved: {sorted(engineering)}")

    policy = {t for t in blockers if t.startswith("billing.")}
    assert policy == {
        "billing.entitlement", "billing.invoice",
        "billing.payment_method_ref", "billing.subscription",
    }, "the billing policy set changed without a decision being recorded"

    # PRIVACY_POLICY_LAUNCH_READY is FALSE while any of these stands. Engineers
    # do not get to answer them, so the gate records them rather than resolving
    # them: billing retention, PD-3 (auth retention duration), PD-10 (deployed
    # versioned-object-storage erasure).
    assert policy, "launch readiness must stay blocked while billing is undecided"


def test_the_app_role_still_cannot_delete_an_account():
    """The half of 11B6I's terminal-removal line that never moves.

    11B6I asserted that NO terminal-removal verb existed. Entry 11B6J creates
    exactly one — `identity.terminal_remove_account` — so that half of the
    assertion is now the previous entry's history rather than a live invariant,
    and it lives in `test_terminal_account_removal.py` where the keyhole is
    proven.

    What does NOT move is this: the application role holds no DELETE on
    `identity.user_account`, and the only way an account is removed is through a
    SECURITY DEFINER keyhole that is fail-closed on five phases and five zero
    counts. The forbidden verbs 11B6I named alongside it stay forbidden.
    """
    connection = psycopg2.connect(owner_dsn())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_table_privilege('onyx_app_rw',"
                " 'identity.user_account', 'DELETE')")
            assert cursor.fetchone()[0] is False, (
                "DELETE on identity.user_account was restored to the app role")
            cursor.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n"
                " ON n.oid = p.pronamespace"
                " WHERE p.proname IN ('complete_account_deletion',"
                " 'delete_account_final')")
            assert cursor.fetchone()[0] == 0, (
                "a verb 11B6I forbade by name was created")
    finally:
        connection.close()


async def test_the_diagnostic_terminal_census_shows_detail_gone_evidence_kept():
    """DIAGNOSTIC TERMINAL-STATE CENSUS — not product terminal deletion.

    An owner-level DELETE in a disposable database, run only after the cleanup
    phase reports COMPLETE. No terminal keyhole is created and none exists; this
    measures what a future terminal removal would leave behind.
    """
    from app.services.ioe.domain.integrity import EntityType
    from app.services.ioe.replay.verification import IntegrityVerificationService

    uid, run_id, portfolio_id, scenario_id, token = await _prepared_account()
    c = _owner()
    cur = c.cursor()
    try:
        kept_before = {}
        for table, pred in _VERIFICATION_SCOPE.items():
            cur.execute(f"SELECT count(*) FROM {table} WHERE {pred}", {"u": str(uid)})
            kept_before[table] = cur.fetchone()[0]

        _start(cur, uid, token)
        _purge(cur, uid, token)
        assert _remaining(cur, uid) == 0
        assert _complete(cur, uid, token) is True

        # every purge-set table is empty for this account
        for table in sorted(implemented_purge_set()):
            cur.execute(f"SELECT count(*) FROM {table} WHERE {_SCOPE[table]}",
                        {"u": str(uid)})
            assert cur.fetchone()[0] == 0, f"{table} survived the cleanup"

        # The diagnostic removal itself. The lifecycle rows are deliberately
        # left alone: nothing joins them to `identity.user_account` by foreign
        # key, and they are the durable record that the deletion happened (PD-9)
        # — `account_lifecycle_phase` even refuses DELETE outright.
        cur.execute("DELETE FROM identity.user_account WHERE id=%s", (str(uid),))
        assert cur.rowcount == 1, "the diagnostic account removal did not happen"
        cur.execute("SELECT count(*) FROM identity.account_lifecycle WHERE user_id=%s",
                    (str(uid),))
        assert cur.fetchone()[0] == 1, "the deletion record did not outlive the account"

        # Retained evidence outlives the account. Asserted as "whatever was
        # there before is still there", not "these tables are non-empty":
        # portfolio assembly admits no candidate once the shared rule landscape
        # is saturated, so `portfolio_member` is legitimately empty on some runs
        # and a non-emptiness assertion would fail for a reason that has nothing
        # to do with the cleanup.
        for table, pred in _VERIFICATION_SCOPE.items():
            cur.execute(f"SELECT count(*) FROM {table} WHERE {pred}", {"u": str(uid)})
            assert cur.fetchone()[0] == kept_before[table], (
                f"{table} changed across the cleanup and the account removal")
        assert sum(kept_before.values()) > 0, "no retained evidence was exercised"
    finally:
        c.close()

    # ENTRY 12B1 PHASE B. Optimization and portfolio still verify — their
    # verification-required tables survive. A v2 SCENARIO does not, and that is
    # the deliberate cost of Decision B: `ioe.scenario_result` carries the
    # counterfactual derived state a v2 seal binds, and it is purged rather than
    # retained, because keeping derived tax results so a deleted account stays
    # replayable would put replay above the deletion the user asked for.
    #
    # The requirement is therefore UNAVAILABLE, never MISMATCH — an authorized
    # purge must not be reportable as tampering.
    svc = IntegrityVerificationService(uid)
    for kind, eid in ((EntityType.OPTIMIZATION, run_id),
                      (EntityType.PORTFOLIO, portfolio_id)):
        result = await svc.verify(kind, eid)
        assert str(result.status) == "verified", (
            f"{kind} stopped verifying: {result.status}/{result.reason_code}")

    scenario_result = await svc.verify(EntityType.SCENARIO, scenario_id)
    assert str(scenario_result.status) == "unavailable", (
        f"a purged v2 scenario reported {scenario_result.status}/"
        f"{scenario_result.reason_code}; it must fail closed as unavailable")
    assert str(scenario_result.reason_code) == "SEALED_EVIDENCE_INCOMPLETE"
    assert str(scenario_result.status) != "mismatch"


async def test_a_late_direct_writer_cannot_recreate_detail_after_complete():
    """THE GAP 11B6I LEFT OPEN, NOW CLOSED (migration 0065).

    After the cleanup phase reported COMPLETE, a writer holding INSERT could put
    the purged detail straight back. Through the product that was unreachable —
    the engine runs only for an authenticated user and a deleting account is
    ACCESS_DISABLED — but "unreachable through the paths we thought of" is not a
    guarantee, and this asserts the guarantee.

    The refusal starts at the deletion REQUEST, not at phase completion: a write
    landing before the phase would otherwise be purged silently, and one landing
    after would resurrect detail. Refusing from the request covers both.
    """
    uid, run_id, _pf, _sc, token = await _prepared_account()
    c = _owner()
    cur = c.cursor()
    try:
        _start(cur, uid, token)
        _purge(cur, uid, token)
        assert _complete(cur, uid, token) is True
        assert _remaining(cur, uid) == 0

        with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
            cur.execute(
                "INSERT INTO ioe.optimization_run_event (run_id, to_status, reason_code)"
                " VALUES (%s, 'completed', 'PROBE')", (str(run_id),))
        assert "being deleted" in str(excinfo.value)

        assert _remaining(cur, uid) == 0, "a refused insert still landed"
        assert _phase_status(cur, uid) == "COMPLETE"
    finally:
        c.close()


async def test_the_write_cutoff_covers_every_purged_table():
    """Every table the keyhole empties refuses writes for a deleting account.

    Asserted against the IMPLEMENTED purge set, so a table added to the keyhole
    without a cutoff fails here rather than becoming a quiet resurrection route.
    """
    connection = psycopg2.connect(owner_dsn())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT n.nspname || '.' || c.relname"
                "  FROM pg_trigger t"
                "  JOIN pg_class c ON c.oid = t.tgrelid"
                "  JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE t.tgname = 'trg_write_cutoff' AND NOT t.tgisinternal")
            covered = {row[0] for row in cursor.fetchall()}
    finally:
        connection.close()

    missing = sorted(implemented_purge_set() - covered)
    assert missing == [], f"purged with no write cutoff: {missing}"

    # And it must NOT reach the retained evidence: those are never purged, so a
    # cutoff there would refuse writes a sealed artifact legitimately needs.
    overreach = sorted(covered & set(VERIFICATION_READS))
    assert overreach == [], f"the cutoff reaches retained evidence: {overreach}"


async def test_an_ordinary_account_is_unaffected_by_the_cutoff():
    """The guard-on-the-guard: refusing everything would pass the test above."""
    uid, run_id, _pf, _sc = await _sealed_chain()
    c = _owner()
    cur = c.cursor()
    try:
        cur.execute(
            "INSERT INTO ioe.optimization_run_event (run_id, to_status, reason_code)"
            " VALUES (%s, 'completed', 'PROBE')", (str(run_id),))
        assert cur.rowcount == 1, "an ordinary account could not write its own detail"
    finally:
        c.close()


# ------------------------------------------------- in-flight atomicity --
def _request_deletion(cur, uid) -> None:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state)"
                " VALUES (%s,'DELETION_REQUESTED')"
                " ON CONFLICT (user_id) DO NOTHING", (str(uid),))


async def _fresh_analysis(user_id) -> uuid.UUID:
    """A new analysis over CHANGED financials, so the next optimization is a
    genuinely new specification.

    `generate` resolves idempotency in TX-1 against the SPECIFICATION hash, not
    the analysis id — measured. A second analysis over identical figures
    produces an identical frozen snapshot, an identical spec hash, and TX-1
    hands back the existing completed run without ever entering TX-2. Two
    earlier versions of this test did exactly that and reported DID NOT RAISE
    while never exercising the path.

    Changing the income is what makes the spec differ, and it is also what a
    real second optimization would be for.
    """
    from decimal import Decimal

    from sqlalchemy import select as _select

    from app.database.models import IncomeSource, IncomeType
    from app.services.analysis.service import AnalysisService

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        income_type_id = (await session.scalar(
            _select(IncomeType).where(IncomeType.code == "employment"))).id
        session.add(IncomeSource(
            user_id=user_id, tax_year=2025, income_type_id=income_type_id,
            amount=Decimal("7331.00"), province_code="ON"))

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        return (await AnalysisService(session).run(user_id, 2025)).id


def _artifact_counts(cur, uid) -> dict[str, int]:
    """Everything an optimization persists, counted per account."""
    out: dict[str, int] = {}
    cur.execute("SELECT count(*) FROM ioe.optimization_run WHERE user_id=%s",
                (str(uid),))
    out["optimization_run"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM ioe.optimization_run WHERE user_id=%s"
                "   AND optimization_result_hash IS NOT NULL", (str(uid),))
    out["sealed_runs"] = cur.fetchone()[0]
    # Events belonging to a SEALED run. TX-1 writes events for headers that may
    # never seal, so "no new events at all" is the wrong invariant; "nothing was
    # appended to a sealed artifact" is the right one.
    cur.execute("SELECT count(*) FROM ioe.optimization_run_event e"
                "  JOIN ioe.optimization_run r ON r.id = e.run_id"
                " WHERE r.user_id = %s AND r.optimization_result_hash IS NOT NULL",
                (str(uid),))
    out["events_on_sealed_runs"] = cur.fetchone()[0]
    for table, pred in _VERIFICATION_SCOPE.items():
        cur.execute(f"SELECT count(*) FROM {table} WHERE {pred}", {"u": str(uid)})
        out[table] = cur.fetchone()[0]
    for table in sorted(implemented_purge_set()):
        cur.execute(f"SELECT count(*) FROM {table} WHERE {_SCOPE[table]}",
                    {"u": str(uid)})
        out[table] = cur.fetchone()[0]
    return out


async def test_a_deletion_requested_mid_computation_leaves_no_partial_artifact():
    """THE TRANSACTION-BOUNDARY PROOF the write cutoff makes necessary.

    The cutoff refuses writes to the fifteen purgeable tables and deliberately
    does NOT refuse writes to the eight verification-required ones. That raises
    a real question: if deletion is requested WHILE an optimization is
    computing, can the retained half commit before the purgeable half is
    refused, leaving a half-written sealed artifact?

    Measured against the production path. `OptimizationOrchestrator.generate` is
    TX-1 (header) -> compute (no transaction) -> TX-2 (`_persist`: "one atomic
    transaction: all children, the sealed hash, and completion"). Deletion is
    requested at the moment the engine finishes, so persistence runs entirely
    after the cutoff is live — the worst case for this question.

    The account already holds VALID SEALED HISTORY, so the assertions are
    deltas. Asserting "the tables are empty" would pass for the wrong reason and
    would also be false.
    """
    uid, run_id, portfolio_id, scenario_id = await _sealed_chain()

    c = _owner()
    cur = c.cursor()
    try:
        cur.execute("SELECT optimization_result_hash FROM ioe.optimization_run"
                    " WHERE id=%s", (str(run_id),))
        sealed_hash_before = cur.fetchone()[0]

        # A GENUINELY NEW SPECIFICATION. Re-running `generate` against the same
        # analysis returns TX-1's existing completed run by idempotency and
        # never reaches TX-2.
        analysis_id = await _fresh_analysis(uid)

        # BASELINE TAKEN HERE, NOT EARLIER. `_fresh_analysis` legitimately writes
        # its own analysis detail before any deletion is requested; measuring
        # from before it would attribute that lawful work to the failed
        # optimization and fail for the wrong reason.
        before = _artifact_counts(cur, uid)
        assert before["sealed_runs"] >= 1, "fixture produced no sealed history"

        from app.services.ioe.orchestrator import OptimizationOrchestrator

        orchestrator = OptimizationOrchestrator(uid)
        original_compute = orchestrator._compute

        async def compute_then_request_deletion(*args, **kwargs):
            computed = await original_compute(*args, **kwargs)
            # The user asks to be deleted while the engine is running. TX-1 has
            # already committed a header; TX-2 has not started.
            _request_deletion(cur, uid)
            return computed

        orchestrator._compute = compute_then_request_deletion

        with pytest.raises(Exception) as excinfo:
            await orchestrator.generate(analysis_id)
        assert "being deleted" in str(excinfo.value) or "deleted" in str(excinfo.value), (
            f"persistence failed for an unrelated reason: {excinfo.value}")

        after = _artifact_counts(cur, uid)

        # NOTHING TX-2 WOULD HAVE WRITTEN SURVIVES, in either half. This is the
        # invariant that matters: the retained and the purgeable halves live in
        # the same transaction, so a refusal in one aborts the other.
        for table in sorted(implemented_purge_set() - {"ioe.optimization_run_event"}):
            assert after[table] == before[table], (
                f"{table}: purge-detail rows survived a refused persistence")
        for table in _VERIFICATION_SCOPE:
            assert after[table] == before[table], (
                f"{table}: verification-required rows committed while the "
                f"purgeable half was refused — that is a partial artifact")
        assert after["sealed_runs"] == before["sealed_runs"], (
            "a new sealed result hash survived a refused persistence")

        # `ioe.optimization_run_event` IS excepted above, and this is why —
        # measured, not waved through. TX-1 commits the header and its
        # pending/running workflow events BEFORE the engine runs, which is
        # before the user asked to be deleted. Those are lawful pre-cutoff
        # writes, not a torn artifact. What must be true is that every one of
        # them belongs to a run that never sealed: no hash, no children, and
        # `SealedEvidenceIncomplete` from any replay — the same state every
        # failed run has always left behind.
        assert after["events_on_sealed_runs"] == before["events_on_sealed_runs"], (
            "an event was appended to a SEALED artifact by a refused "
            "persistence")
        cur.execute(
            "SELECT count(*) FROM ioe.optimization_run"
            " WHERE user_id = %s AND workflow_status = 'completed'"
            "   AND optimization_result_hash IS NULL", (str(uid),))
        assert cur.fetchone()[0] == 0, (
            "a run is marked completed with no sealed hash — that IS a torn "
            "artifact")

        # The pre-existing history is untouched.
        cur.execute("SELECT optimization_result_hash FROM ioe.optimization_run"
                    " WHERE id=%s", (str(run_id),))
        assert cur.fetchone()[0] == sealed_hash_before

        # TX-1's header is the ONE thing that legitimately survives: it committed
        # before the cutoff existed. It is not a sealed artifact — no hash, no
        # children — and replay refuses it as SealedEvidenceIncomplete, which is
        # exactly how every failed run has always been modelled.
        assert after["optimization_run"] - before["optimization_run"] <= 1, (
            "more than one unsealed header survived")
    finally:
        c.close()

    # The pre-existing sealed evidence still verifies.
    from app.services.ioe.domain.integrity import EntityType
    from app.services.ioe.replay.verification import IntegrityVerificationService

    service = IntegrityVerificationService(uid)
    for kind, entity_id in ((EntityType.OPTIMIZATION, run_id),
                            (EntityType.PORTFOLIO, portfolio_id),
                            (EntityType.SCENARIO, scenario_id)):
        result = await service.verify(kind, entity_id)
        assert str(result.status) == "verified", (
            f"{kind} broke after a refused in-flight persistence: "
            f"{result.status}/{result.reason_code}")


async def test_the_lifecycle_still_converges_after_a_refused_in_flight_write():
    """A failed computation must not wedge deletion.

    If a refused in-flight persistence left the account in a state the cleanup
    phase could not complete, the cutoff would have traded a resurrection hole
    for a permanently stuck deletion. It does not: the refused transaction wrote
    nothing, so there is nothing left to purge.
    """
    uid, _run, _pf, _sc = await _sealed_chain()
    c = _owner()
    cur = c.cursor()
    try:
        analysis_id = await _fresh_analysis(uid)

        from app.services.ioe.orchestrator import OptimizationOrchestrator

        orchestrator = OptimizationOrchestrator(uid)
        original_compute = orchestrator._compute

        async def compute_then_request_deletion(*args, **kwargs):
            computed = await original_compute(*args, **kwargs)
            _request_deletion(cur, uid)
            return computed

        orchestrator._compute = compute_then_request_deletion
        with pytest.raises(Exception, match="deleted"):
            await orchestrator.generate(analysis_id)

        # Now run the deletion the user actually asked for.
        for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
            cur.execute("UPDATE identity.account_lifecycle SET state=%s"
                        " WHERE user_id=%s", (state, str(uid)))
        _mark_earlier_complete(cur, uid)
        token = _claim(cur, uid)

        _start(cur, uid, token)
        _purge(cur, uid, token)
        assert _remaining(cur, uid) == 0
        assert _complete(cur, uid, token) is True
        assert _phase_status(cur, uid) == "COMPLETE"
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Entry 12B1 Phase B — the governed lifecycle exception, proved in three states
# ---------------------------------------------------------------------------
async def _sealed_v2_scenario():
    """One genuinely sealed v2 scenario, through the service that seals them."""
    from decimal import Decimal

    from app.services.analysis.service import AnalysisService
    from app.services.ioe.domain.scenario import (
        SCENARIO_RESULT_SCHEMA_V2,
        ScenarioSpec,
    )
    from app.services.ioe.scenario.service import ScenarioService
    from tests.security.test_sealed_history_after_purge import (
        _LEVERS,
        _live_account,
        _publish,
    )

    tag = uuid.uuid4().hex[:8].upper()
    for i in range(2):
        lever, resource = _LEVERS[i % len(_LEVERS)]
        await _publish(f"LX{tag}_{i}", lever, str(2000 + i * 400), resource)

    user_id = await _live_account()
    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        analysis_id = (await AnalysisService(session).run(user_id, 2025)).id

    outcome = await ScenarioService(user_id)._simulate(
        analysis_id,
        ScenarioSpec.parse([{"lever_code": "INCREASE_RRSP_DEDUCTION",
                             "parameters": {"amount": Decimal("5000")}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
    )
    return user_id, outcome.scenario_id


async def _verify_scenario(user_id, scenario_id):
    from app.services.ioe.domain.integrity import EntityType
    from app.services.ioe.replay.verification import IntegrityVerificationService

    return await IntegrityVerificationService(user_id).verify(
        EntityType.SCENARIO, scenario_id)


async def test_a_healthy_v2_scenario_verifies():
    """STATE 1 of the three-state proof. Without this the other two states
    prove nothing — a scenario that never verified cannot demonstrate what
    breaks it."""
    user_id, scenario_id = await _sealed_v2_scenario()
    result = await _verify_scenario(user_id, scenario_id)
    assert str(result.status) == "verified", (result.status, result.reason_code)


async def test_unexpected_damage_is_not_disguised_as_a_lifecycle_purge():
    """STATE 2. Evidence mutated OUTSIDE the governed workflow, with the account
    perfectly alive.

    This is the case the whole exception must not swallow. A row that was
    tampered with must keep reporting through the ordinary integrity taxonomy;
    "something is missing" must never be read as "deletion did it".
    """
    import json

    user_id, scenario_id = await _sealed_v2_scenario()
    assert str((await _verify_scenario(user_id, scenario_id)).status) == "verified"

    connection = _owner()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT counterfactual_derived_state FROM ioe.scenario_result "
                "WHERE scenario_id = %s", (str(scenario_id),))
            payload = cursor.fetchone()[0]
            payload["line_items"] = [
                {**payload["line_items"][0], "amount": "999999.99"},
                *payload["line_items"][1:],
            ]
            cursor.execute("SET session_replication_role = replica")
            cursor.execute(
                "UPDATE ioe.scenario_result SET counterfactual_derived_state = "
                "%s::jsonb WHERE scenario_id = %s",
                (json.dumps(payload), str(scenario_id)))
            cursor.execute("SET session_replication_role = origin")
    finally:
        connection.close()

    result = await _verify_scenario(user_id, scenario_id)
    assert str(result.status) != "verified"
    # the account is alive and no purge ran, so this must NOT read as lifecycle
    # deletion — the sealed row is present and simply does not reconcile
    assert str(result.status) == "mismatch", (
        f"tampering with a live account's sealed evidence reported "
        f"{result.status}/{result.reason_code}; damage must stay "
        "distinguishable from an authorized purge")


async def test_an_authorized_purge_yields_unavailable_not_mismatch():
    """STATE 3 — THE LOAD-BEARING TEST OF DECISION B.

    The real governed workflow runs: lifecycle walked to PURGING, the phase
    claimed and started, `identity.purge_historical_detail` executed through the
    keyhole. `ioe.scenario_result` is genuinely destroyed — no copy, no archive,
    no retention exception.

    Verification must then say UNAVAILABLE. Not MISMATCH: the original 11B6
    invariant existed to stop an authorized cleanup being reported as tampering,
    and that purpose survives this narrowing intact.
    """
    from app.privacy.classification import (
        LIFECYCLE,
        POST_DELETION_STRUCTURED_UNAVAILABLE,
    )

    user_id, scenario_id = await _sealed_v2_scenario()
    assert str((await _verify_scenario(user_id, scenario_id)).status) == "verified"

    connection = _owner()
    cursor = connection.cursor()
    try:
        _walk_to_purging(cursor, user_id)
        _mark_earlier_complete(cursor, user_id)
        token = _claim(cursor, user_id)
        _start(cursor, user_id, token)
        _purge(cursor, user_id, token)

        cursor.execute(
            "SELECT count(*) FROM ioe.scenario_result WHERE scenario_id = %s",
            (str(scenario_id),))
        surviving = cursor.fetchone()[0]
    finally:
        connection.close()

    assert surviving == 0, (
        "the authorized purge did not remove ioe.scenario_result; Decision B "
        "preserves the deletion guarantee and this is that guarantee")

    result = await _verify_scenario(user_id, scenario_id)
    assert str(result.status) == "unavailable", (
        f"an authorized lifecycle purge reported {result.status}/"
        f"{result.reason_code}; it must fail closed as unavailable")
    assert str(result.status) != "mismatch"
    assert str(result.reason_code) == "SEALED_EVIDENCE_INCOMPLETE", (
        result.reason_code)

    # and the classification declared exactly this outcome in advance
    assert LIFECYCLE["ioe.scenario_result"].post_account_deletion_replay == (
        POST_DELETION_STRUCTURED_UNAVAILABLE)


async def test_a_purged_scenario_is_not_reconstructed_from_live_state():
    """§9. UNAVAILABLE is the answer; rebuilding is not.

    After the purge the engine, the rules evaluator and the document library are
    all still perfectly capable of producing *a* counterfactual state. Using any
    of them would manufacture evidence for a deleted account and report it as
    verified history.
    """
    from sqlalchemy import event

    user_id, scenario_id = await _sealed_v2_scenario()

    connection = _owner()
    cursor = connection.cursor()
    try:
        _walk_to_purging(cursor, user_id)
        _mark_earlier_complete(cursor, user_id)
        token = _claim(cursor, user_id)
        _start(cursor, user_id, token)
        _purge(cursor, user_id, token)
    finally:
        connection.close()

    statements: list[str] = []

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        result = await _verify_scenario(user_id, scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert str(result.status) == "unavailable"
    repair_reads = [s for s in statements if "docs.document" in s]
    assert repair_reads == [], (
        f"verification tried to rebuild purged evidence from the live document "
        f"library: {repair_reads[:1]}")
    assert not [s for s in statements if "insert into ioe.scenario_result" in s], (
        "verification attempted to re-create the purged artifact")
