"""Entry 11B6H — what integrity verification ACTUALLY reads, measured twice.

Twenty `ioe` sealed-detail tables and three `analysis` ones sat
`UNCLASSIFIED_BLOCKING` behind a single question: does anything read them once
the account is gone? Entry 11B6G recorded the answer as "replay reads none of
the 20". **That was wrong**, and this module is the measurement that corrects
it: verification reads exactly EIGHT of the twenty-three, and each of the eight
is load-bearing — deleting its rows turns a verified artifact into a mismatch or
an unverifiable one.

Two independent methods, because either alone has a hole:

  * `test_verification_reads_exactly_the_measured_set` traces every SQL
    statement the three production verifications issue. It does not depend on a
    fixture populating a table, so a table this suite never fills is still
    correctly reported as unread.
  * `test_deleting_a_read_table_breaks_verification` deletes each of the eight
    and re-verifies. Reading a table does not prove the read MATTERS; this does.

The declaration `replay_dependency` is asserted against the traced set rather
than against a hand-maintained list, so the two cannot drift apart.

WHY THE OLD ANSWER WAS WRONG. `app/privacy/classification.py` set
`replay_dependency=True` for every sealed child in one comprehension. That
encodes "is descended from a replayable run" — lineage — while the field's
docstring promises "does replaying a sealed historical result need this row?" —
consumption. Eleven tables were declared True that nothing reads, and the
inverse error was equally possible: `ioe.run_rule_version` really is read, by
`ReplayDependencyResolver.pinned_rule_versions`, and a purge built on the old
census would have destroyed it.
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import event, select

from app.database.models import OptimizationRun, StrategyPortfolio
from app.database.session import unit_of_work
from app.privacy.classification import LIFECYCLE
from app.services.analysis.service import AnalysisService
from app.services.ioe.domain.integrity import EntityType
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.replay.resolver import ReplayDependencyResolver
from app.services.ioe.replay.verification import IntegrityVerificationService
from app.services.ioe.scenario.service import ScenarioService
from tests.conftest import owner_dsn
from tests.security.test_sealed_history_after_purge import (
    _LEVERS,
    _live_account,
    _publish,
)

#: The twenty-three engineering blockers 11B6H set out to resolve.
BLOCKERS = (
    "ioe.candidate_cost", "ioe.candidate_economic_effect",
    "ioe.confidence_component", "ioe.multi_year_projection",
    "ioe.optimization_candidate", "ioe.optimization_run_event",
    "ioe.portfolio_evaluation_step", "ioe.portfolio_exclusion",
    "ioe.portfolio_member", "ioe.recommendation_relationship",
    "ioe.resource_ledger_entry", "ioe.run_rule_version",
    "ioe.scenario_assumption", "ioe.scenario_confidence_component",
    "ioe.scenario_event", "ioe.scenario_input_change", "ioe.scenario_lever",
    "ioe.scenario_result", "ioe.score_component", "ioe.strategy_portfolio",
    "analysis.analysis_assumption", "analysis.analysis_line_item",
    "analysis.reconciliation_check",
)

#: MEASURED, not asserted from lineage. Every member is read by
#: `IntegrityVerificationService.verify` and every non-member is not.
#:
#: THIS SET IS VERSION-DEPENDENT. Below is what EVERY verification reads,
#: whatever schema version the artifact was sealed under;
#: `VERIFICATION_READS_WITH_DERIVED_STATE` adds the one table a scenario sealed
#: under a derived-state-bearing contract additionally needs. Both are traced, and
#: `test_replay_dependency_declares_consumption_not_lineage` holds the privacy
#: classification to whichever matches the version production actually writes —
#: so the census and the purge model cannot drift apart when that version moves.
VERIFICATION_READS_V1 = frozenset({
    "ioe.optimization_candidate",
    "ioe.portfolio_exclusion",
    "ioe.portfolio_member",
    "ioe.resource_ledger_entry",
    "ioe.run_rule_version",
    "ioe.scenario_assumption",
    "ioe.scenario_lever",
    "ioe.strategy_portfolio",
})

#: What a verification reads on top of the v1 set once the sealed contract
#: BINDS a counterfactual derived state. Measured by
#: `test_a_v2_verification_additionally_reads_the_result_row`.
#:
#: Named for the property rather than for one version: v2 and v3 both bind that
#: state and both therefore read the row, and a set named after a single
#: version would have gone quietly wrong the moment a third one existed.
VERIFICATION_READS_WITH_DERIVED_STATE = (
    VERIFICATION_READS_V1 | {"ioe.scenario_result"})


def _verification_reads_for_production() -> frozenset[str]:
    """The census the privacy classification must match TODAY.

    Keyed off the single current-write authority rather than a hand-maintained
    constant, so moving the write version moves the expected consumer set with
    it and the classification assertion fails until privacy is updated to match.
    """
    from app.services.ioe.domain.scenario import (
        CURRENT_SCENARIO_RESULT_SCHEMA_VERSION,
        DERIVED_STATE_BEARING_VERSIONS,
    )

    if CURRENT_SCENARIO_RESULT_SCHEMA_VERSION in DERIVED_STATE_BEARING_VERSIONS:
        return frozenset(VERIFICATION_READS_WITH_DERIVED_STATE)
    return frozenset(VERIFICATION_READS_V1)


#: The v1-shaped set, under the name the older assertions in this file use.
VERIFICATION_READS = VERIFICATION_READS_V1

#: How to reach one account's rows in each read table. Deletion is scoped to the
#: account under test so a failure cannot damage another test's fixtures.
_SCOPE = {
    "ioe.strategy_portfolio":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id = %(u)s)",
    "ioe.optimization_candidate":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id = %(u)s)",
    "ioe.run_rule_version":
        "run_id IN (SELECT id FROM ioe.optimization_run WHERE user_id = %(u)s)",
    "ioe.portfolio_member":
        "portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p "
        " JOIN ioe.optimization_run r ON r.id = p.run_id WHERE r.user_id = %(u)s)",
    "ioe.portfolio_exclusion":
        "portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p "
        " JOIN ioe.optimization_run r ON r.id = p.run_id WHERE r.user_id = %(u)s)",
    "ioe.resource_ledger_entry":
        "portfolio_id IN (SELECT p.id FROM ioe.strategy_portfolio p "
        " JOIN ioe.optimization_run r ON r.id = p.run_id WHERE r.user_id = %(u)s)",
    "ioe.scenario_lever":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id = %(u)s)",
    "ioe.scenario_assumption":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id = %(u)s)",
    "ioe.scenario_result":
        "scenario_id IN (SELECT id FROM ioe.scenario WHERE user_id = %(u)s)",
}


class _SqlWatch:
    """Records every statement the application engine executes."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def __enter__(self) -> _SqlWatch:
        from app.database.session import engine

        self._sync = engine.sync_engine

        @event.listens_for(self._sync, "before_cursor_execute")
        def record(conn, cursor, statement, parameters, context, executemany):
            self.sql.append(statement)

        self._fn = record
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._sync, "before_cursor_execute", self._fn)

    def read_tables(self) -> set[str]:
        hit = set()
        for table in BLOCKERS:
            schema, name = table.split(".")
            pattern = re.compile(rf"\b{schema}\.{name}\b")
            for statement in self.sql:
                if pattern.search(statement) and statement.lstrip().upper().startswith(
                    ("SELECT", "WITH")
                ):
                    hit.add(table)
                    break
        return hit


async def _sealed_chain() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A production-sealed analysis -> optimization -> portfolio -> scenario.

    Built by the services that build it in production. The scenario carries
    ASSUMPTIONS as well as levers, because `ioe.scenario_assumption` is one of
    the tables under test and a spec without assumptions would leave it empty —
    which reads as "nothing needs it" for entirely the wrong reason.
    """
    tag = uuid.uuid4().hex[:8].upper()
    for i in range(3):
        lever, resource = _LEVERS[i % len(_LEVERS)]
        await _publish(f"VC{tag}_{i}", lever, str(2000 + i * 400), resource)

    user_id = await _live_account()
    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        analysis_id = (await AnalysisService(session).run(user_id, 2025)).id

    outcome = await OptimizationOrchestrator(user_id).generate(analysis_id)
    scenario = await ScenarioService(user_id).simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": "INCREASE_RRSP_DEDUCTION",
              "parameters": {"amount": Decimal("5000")}}],
            assumptions=[
                {"assumption_code": "EMPLOYMENT_INCOME_CONSTANT",
                 "value_boolean": True},
                {"assumption_code": "PROVINCE_UNCHANGED", "value_boolean": True},
            ],
        ),
    )
    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        portfolio_id = await session.scalar(
            select(StrategyPortfolio.id).where(
                StrategyPortfolio.run_id == outcome.run_id)
        )
    return user_id, outcome.run_id, portfolio_id, scenario.scenario_id


async def _verify_all(user_id, run_id, portfolio_id, scenario_id) -> dict[str, str]:
    service = IntegrityVerificationService(user_id)
    out: dict[str, str] = {}
    for label, kind, entity_id in (
        ("OPTIMIZATION", EntityType.OPTIMIZATION, run_id),
        ("PORTFOLIO", EntityType.PORTFOLIO, portfolio_id),
        ("SCENARIO", EntityType.SCENARIO, scenario_id),
    ):
        try:
            result = await service.verify(kind, entity_id)
            out[label] = f"{result.status}/{result.reason_code}"
        except Exception as exc:  # noqa: BLE001
            out[label] = f"RAISED:{type(exc).__name__}"
    return out


_ALL_VERIFIED = {
    "OPTIMIZATION": "verified/NONE",
    "PORTFOLIO": "verified/NONE",
    "SCENARIO": "verified/NONE",
}


async def test_verification_reads_exactly_the_measured_set():
    """Trace the SQL. Independent of whether a fixture populated the table."""
    user_id, run_id, portfolio_id, scenario_id = await _sealed_chain()

    # `PortfolioReplayService._candidate_keys` returns early when a portfolio has
    # neither members nor exclusions, and that is the one read in the set which
    # is conditional. Assert the artifact can exercise it rather than letting a
    # totally empty portfolio quietly shrink the measured set.
    assert (
        _count_scoped("ioe.portfolio_member", user_id)
        + _count_scoped("ioe.portfolio_exclusion", user_id)
    ) > 0, "portfolio has neither members nor exclusions — nothing to trace"

    read: set[str] = set()
    for kind, entity_id in (
        (EntityType.OPTIMIZATION, run_id),
        (EntityType.PORTFOLIO, portfolio_id),
        (EntityType.SCENARIO, scenario_id),
    ):
        with _SqlWatch() as watch:
            result = await IntegrityVerificationService(user_id).verify(kind, entity_id)
        # A verification that did not succeed proves nothing about what a
        # successful one reads.
        assert str(result.status) == "verified", (kind, result.status, result.reason_code)
        read |= watch.read_tables()

    expected = _verification_reads_for_production()
    assert read == set(expected), (
        f"the set of blocker tables verification reads has changed: "
        f"newly read {sorted(read - set(expected))}, "
        f"no longer read {sorted(set(expected) - read)}"
    )


async def test_a_v2_verification_additionally_reads_the_result_row():
    """THE PENDING CENSUS CHANGE, measured now rather than at activation.

    A v2 scenario binds a counterfactual derived state into its result hash, so
    verifying one reads `ioe.scenario_result` — first for the rule-version set
    the state was evaluated over, then for the state itself. That is one more
    blocker table than the v1 census above records.

    Production writes v1, so the census is still accurate today, and the reads
    are guarded on the sealed version so they cannot leak into a v1
    verification. This test exists so that stops being true LOUDLY: when
    `CURRENT_SCENARIO_RESULT_SCHEMA_VERSION` moves to v2,
    `ioe.scenario_result` has to be reclassified `replay_dependency=True`
    first, or a purge built on the v1 census will destroy a row that
    verification needs.
    """
    from app.services.ioe.domain.scenario import SCENARIO_RESULT_SCHEMA_V2

    tag = uuid.uuid4().hex[:8].upper()
    for i in range(2):
        lever, resource = _LEVERS[i % len(_LEVERS)]
        await _publish(f"V2C{tag}_{i}", lever, str(2000 + i * 400), resource)

    user_id = await _live_account()
    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        analysis_id = (await AnalysisService(session).run(user_id, 2025)).id

    outcome = await ScenarioService(user_id)._simulate(
        analysis_id,
        ScenarioSpec.parse([{"lever_code": "INCREASE_RRSP_DEDUCTION",
                             "parameters": {"amount": Decimal("5000")}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
    )

    with _SqlWatch() as watch:
        result = await IntegrityVerificationService(user_id).verify(
            EntityType.SCENARIO, outcome.scenario_id)
    assert str(result.status) == "verified", (result.status, result.reason_code)

    v2_read = watch.read_tables()
    assert "ioe.scenario_result" in v2_read, (
        "a v2 verification did not read the row carrying its derived state")
    assert v2_read - set(VERIFICATION_READS) == {"ioe.scenario_result"}, (
        f"v2 verification reads more than the one extra table this entry "
        f"accounted for: {sorted(v2_read - set(VERIFICATION_READS))}")


def test_the_result_row_is_declared_exactly_when_production_needs_it():
    """THE PERMANENT ACTIVATION INVARIANT.

    Replaces the temporary Phase A2 guard, which asserted "not yet" and was only
    ever right while production wrote v1. The lasting property is the
    biconditional: `ioe.scenario_result` is a declared replay dependency IF AND
    ONLY IF the version production writes actually reads it. Both directions
    matter — declaring it early overstates what is consumed; declaring it late
    lets a purge built on a stale census destroy evidence a v2 verification
    cannot do without.
    """
    from app.services.ioe.domain.scenario import (
        CURRENT_SCENARIO_RESULT_SCHEMA_VERSION,
        DERIVED_STATE_BEARING_VERSIONS,
    )

    # THE REAL QUESTION IS "does the written contract bind a derived state",
    # not "is the written contract v2". Asked as the latter, this gate went
    # false the moment v3 shipped — and would have demanded that a table every
    # v3 verification reads be declassified as a replay dependency.
    production_reads_it = (
        CURRENT_SCENARIO_RESULT_SCHEMA_VERSION in DERIVED_STATE_BEARING_VERSIONS)
    assert LIFECYCLE["ioe.scenario_result"].replay_dependency is (
        production_reads_it), (
        "ioe.scenario_result.replay_dependency does not match the version "
        f"production writes ({CURRENT_SCENARIO_RESULT_SCHEMA_VERSION}); "
        "reclassify the table BEFORE moving the write version, never after"
    )
    assert ("ioe.scenario_result" in _verification_reads_for_production()) is (
        production_reads_it)


def test_replay_dependency_declares_consumption_not_lineage():
    """`replay_dependency` must equal the measured read set over the blockers.

    This is the assertion 11B6G was missing. It fails if someone re-adds a
    sealed child to the "True" group because of where it hangs in the schema.
    """
    expected = _verification_reads_for_production()
    declared = {t for t in BLOCKERS if LIFECYCLE[t].replay_dependency}
    assert declared == set(expected), (
        f"declared-but-unread {sorted(declared - set(expected))}, "
        f"read-but-undeclared {sorted(set(expected) - declared)}"
    )


def _count_scoped(table: str, user_id: uuid.UUID) -> int:
    connection = psycopg2.connect(owner_dsn())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM {table} WHERE {_SCOPE[table]}",
                {"u": str(user_id)},
            )
            return cursor.fetchone()[0]
    finally:
        connection.close()


def _purge_scoped(table: str, user_id: uuid.UUID) -> int:
    """DELETE one account's rows from a sealed-detail table, as a purge would.

    These tables are insert-only under `ioe.reject_result_mutation`, which
    refuses DELETE outside the sanctioned purge context. The diagnostic borrows
    that context; none of this is product behaviour.
    """
    connection = psycopg2.connect(owner_dsn())
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET app.allow_evidence_purge = 'on'")
            cursor.execute(
                f"DELETE FROM {table} WHERE {_SCOPE[table]}", {"u": str(user_id)}
            )
            return cursor.rowcount
    finally:
        connection.close()


#: `ioe.strategy_portfolio` is excluded here and gets its own case: it cannot be
#: deleted at all, so "delete it and watch verification degrade" is unrunnable.
#: `ioe.scenario_result` is excluded and gets its own v2 case: this fixture
#: seals a v1 scenario, whose verification does not read it, so deleting it here
#: would correctly break nothing and would prove the opposite of the claim.
_DELETABLE_READ_TABLES = sorted(
    VERIFICATION_READS_V1 - {"ioe.strategy_portfolio"})


@pytest.mark.parametrize("table", _DELETABLE_READ_TABLES)
async def test_deleting_a_read_table_breaks_verification(table: str):
    """Necessity, not just readership: each of these is load-bearing.

    A fresh sealed chain per case, and deletion scoped to that account, so no
    case can disturb another and nothing needs restoring.
    """
    user_id, run_id, portfolio_id, scenario_id = await _sealed_chain()
    assert await _verify_all(user_id, run_id, portfolio_id, scenario_id) == _ALL_VERIFIED

    # A DEGENERATE ARTIFACT PROVES NOTHING, AND SAYING SO IS NOT THE SAME AS
    # PASSING. `portfolio_member` and `resource_ledger_entry` are populated only
    # when portfolio assembly actually admits a candidate, and on a shared
    # database that has accumulated hundreds of published rule versions it
    # admits none: measured at ~890 rules, all 886 candidates were excluded and
    # the portfolio came out with 0 members and objective_delta 0.00. That is a
    # property of the rule landscape this suite shares, not of the table under
    # test, so the honest outcome is a skip that names the cause.
    #
    # The unconditional gate is `test_verification_reads_exactly_the_measured_set`,
    # which traces the SQL and does not depend on any table being populated.
    present = _count_scoped(table, user_id)
    if present == 0:
        pytest.skip(
            f"{table} is empty for this account: portfolio assembly admitted no "
            f"candidate, so there is no non-degenerate artifact to destroy. "
            f"Readership is still asserted by the SQL trace test."
        )

    assert _purge_scoped(table, user_id) == present

    after = await _verify_all(user_id, run_id, portfolio_id, scenario_id)
    assert after != _ALL_VERIFIED, (
        f"deleting {table} left every verification green — it is not, in fact, "
        f"read-required, and VERIFICATION_READS is wrong"
    )


async def test_the_sealed_portfolio_cannot_be_deleted_at_all():
    """The strongest retention result of the twenty-three, and a surprise.

    `ioe.strategy_portfolio` is not merely read by verification — it cannot be
    removed even inside the sanctioned purge context, because every verification
    it has ever undergone left an `ioe.integrity_check` row and that table's
    guard refuses DELETE UNCONDITIONALLY (no purge-context escape hatch, unlike
    `ioe.reject_result_mutation`).

    So any future purge phase that tried to include the portfolio would not
    quietly destroy evidence — it would abort. That is the right failure mode,
    and it is worth pinning: weakening the integrity-check guard would silently
    convert this refusal into a successful deletion of verification history.
    """
    user_id, run_id, portfolio_id, scenario_id = await _sealed_chain()
    assert await _verify_all(user_id, run_id, portfolio_id, scenario_id) == _ALL_VERIFIED

    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        _purge_scoped("ioe.strategy_portfolio", user_id)
    assert "append-only" in str(excinfo.value)

    # And the artifact is untouched: still there, still verifying.
    assert await _verify_all(user_id, run_id, portfolio_id, scenario_id) == _ALL_VERIFIED


async def test_the_portfolio_rebuild_orders_exclusions_the_way_the_writer_sealed_them():
    """A REAL DEFECT, found by CI on a fresh database (Entry 11B6I).

    `PortfolioAssemblyState` seals exclusions as `sorted(state.exclusions)` —
    keyed by candidate_key. `PortfolioReplayService._rebuild` read them back with
    an UNORDERED `SELECT` and hashed them in raw fetch order. The two agree only
    by luck; any different plan or page layout reorders the list, and then an
    untampered portfolio reports `PORTFOLIO_HASH_MISMATCH` — the alert whose
    entire meaning is "this evidence was altered".

    It surfaced as a baseline verification failing before any deletion, on CI,
    where the fixture population differs from a saturated local database.

    Asserted as the ordering CONTRACT rather than by trying to provoke a
    particular physical row order, which no test can do reliably.
    """
    from app.services.ioe.replay.services import PortfolioReplayService

    user_id, run_id, portfolio_id, _scenario = await _sealed_chain()

    async with unit_of_work(user_id=user_id, actor_type="user") as session:
        portfolio = await session.get(StrategyPortfolio, portfolio_id)
        deps = await ReplayDependencyResolver(session, user_id).for_optimization(
            await session.get(OptimizationRun, run_id))
        canonical, _runs = await PortfolioReplayService(user_id)._rebuild(
            session, portfolio, deps)

    keys = [e["candidate_key"] for e in canonical["exclusions"]]
    if len(keys) < 2:
        pytest.skip("fewer than two exclusions — ordering is not observable here")
    assert keys == sorted(keys), (
        "the rebuild emitted exclusions in fetch order; the writer sealed them "
        "sorted by candidate_key, so the hash will disagree whenever the two "
        "orders differ"
    )
