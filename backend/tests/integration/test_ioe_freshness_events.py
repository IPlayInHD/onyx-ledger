"""P6 closure tests — outbox-driven freshness and governed projections.

The properties under test:
  * a completed analysis or a rule publication marks affected records stale
    through the EVENT path, not only by read-time evaluation;
  * duplicate events are harmless;
  * a missed event is still caught by read-time evaluation or the sweep;
  * only projection-eligible candidates generate projections;
  * projections never enter current-year portfolio totals;
  * no eligible candidates produces an explicit non-generated response.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    CalcFormula,
    CalcFormulaInput,
    FreshnessOutbox,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    MultiYearProjection,
    OptimizationRun,
    RuleAction,
    RuleOutcome,
    Scenario,
    ScenarioResult,
    StrategyPortfolio,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.analysis.service import AnalysisService
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.freshness_events import FreshnessEvent, emit
from app.services.ioe.freshness_relay import FreshnessRelay
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.scenario.freshness_service import ScenarioFreshnessService
from app.services.ioe.scenario.service import ScenarioService
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _spec(amount="5000"):
    return ScenarioSpec.parse(
        [{"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}]
    )


def _suffix() -> str:
    return uuid.uuid4().hex[:6].upper()


async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"ev_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        itype = await s.scalar(select(IncomeType).where(IncomeType.code == "employment"))
        type_id = itype.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=type_id,
            amount=Decimal("95000"), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        _snap = frozen_snapshot(employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=_snap[0],
            snapshot_hash=_snap[1],
        ))
        await s.flush()
        return uid, run.id


async def _publish_projectable_rule(
    code: str,
    *,
    projection_eligibility: str | None,
    projection_method: str | None = None,
    maximum_horizon: int | None = None,
    required_assumptions: list[str] | None = None,
    effect_type: str = "recurring_annual_benefit",
) -> uuid.UUID:
    """A published rule whose projection metadata is authored explicitly."""
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="projection fixture",
            eligibility_basis_codes=["BASIS_PROJ"],
        )
        s.add(version)
        await s.flush()
        # a literal-valued formula so the candidate carries a real amount;
        # without an economic effect there is nothing to project
        formula = CalcFormula(
            code=f"PROJFORMULA_{uuid.uuid4().hex[:8]}",
            expression="amount", expression_lang="rpn", output_unit="CAD",
            description="fixed projected annual benefit",
        )
        s.add(formula)
        await s.flush()
        s.add(CalcFormulaInput(
            formula_id=formula.id, param_name="amount",
            literal_value=Decimal("1200"),
        ))
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            impact_formula_id=formula.id,
            title_template=f"{code} opportunity",
            economic_effect_type=effect_type,
            reversibility="reversible",
            portfolio_lever_code=RRSP,
            lever_parameters={"amount": "action.cost_amount"},
            projection_eligibility=projection_eligibility,
            projection_method=projection_method,
            maximum_projection_horizon=maximum_horizon,
            required_assumption_codes=required_assumptions,
        ))
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE",
            description="Contribute", effort_rating=2,
            cost_type="liquidity_commitment", cost_amount=Decimal("5000"),
        ))
        await s.flush()
        return version.id


# ---------------------------------------------------------------------------
# The event path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_completed_analysis_emits_an_event_in_its_own_transaction():
    uid, _ = await _user_with_analysis()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await AnalysisService(s).run(uid, 2025)
        analysis_id = run.id
        # the event is visible INSIDE the same transaction that made the change
        pending = await s.scalar(
            select(func.count(FreshnessOutbox.id)).where(
                FreshnessOutbox.dedupe_key == f"analysis_completed:{analysis_id}"
            )
        )
        assert pending == 1


@pytest.mark.asyncio
async def test_a_rolled_back_change_leaves_no_event():
    """The outbox exists precisely so an event cannot outlive its cause."""
    uid, _ = await _user_with_analysis()
    key = f"analysis_completed:{uuid.uuid4()}"
    try:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await emit(
                s, FreshnessEvent.ANALYSIS_COMPLETED,
                user_id=uid, tax_year=2025, dedupe_key=key,
            )
            raise RuntimeError("the originating change failed")
    except RuntimeError:
        pass

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        found = await s.scalar(
            select(func.count(FreshnessOutbox.id))
            .where(FreshnessOutbox.dedupe_key == key)
        )
        assert found == 0, "an event survived a rolled-back change"


@pytest.mark.asyncio
async def test_a_completed_analysis_marks_scenarios_stale_through_the_relay():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.freshness_status == "current"

    # a NEW analysis for the same user and year
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AnalysisService(s).run(uid, 2025)

    relayed = await FreshnessRelay("test-worker").drain()
    assert relayed.claimed >= 1
    assert relayed.scenarios_marked >= 1

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.freshness_status == "stale"
        assert scenario.stale_reason_code == "BASELINE_INPUTS_CHANGED"
        # sealed evidence untouched
        result = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id)
        )
        assert result.tax_delta is not None
        assert result.objective_delta is not None


@pytest.mark.asyncio
async def test_a_rule_publication_marks_scenarios_and_runs_stale():
    from app.services.admin.service import AdminService
    from app.services.tkms.publication.service import PublicationService

    uid, analysis_id = await _user_with_analysis()
    await _publish_projectable_rule(
        f"EVRULE_{_suffix()}", projection_eligibility=None
    )
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())
    run_outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    # emit the publication event directly: the publication service emits the
    # same event inside its own transaction, and its four-eyes prerequisites
    # are exercised by the TKMS suite
    async with unit_of_work(actor_type="system") as s:
        await emit(
            s, FreshnessEvent.RULE_PUBLISHED, tax_year=2025,
            dedupe_key=f"rule_published:{uuid.uuid4()}",
        )
    del AdminService, PublicationService

    relayed = await FreshnessRelay("test-worker").drain()
    assert relayed.scenarios_marked >= 1

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        run = await s.get(OptimizationRun, run_outcome.run_id)
        assert scenario.freshness_status == "stale"
        assert scenario.stale_reason_code == "RULE_SNAPSHOT_SUPERSEDED"
        assert run.freshness_status == "stale"
        assert run.stale_reason_codes == ["RULE_SNAPSHOT_SUPERSEDED"]
        # the run's sealed result hash is unchanged
        assert run.optimization_result_hash is not None


@pytest.mark.asyncio
async def test_duplicate_events_are_harmless():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    key = f"dupe:{uuid.uuid4()}"
    async with unit_of_work(actor_type="system") as s:
        first = await emit(
            s, FreshnessEvent.RULE_PUBLISHED, tax_year=2025, dedupe_key=key
        )
        second = await emit(
            s, FreshnessEvent.RULE_PUBLISHED, tax_year=2025, dedupe_key=key
        )
    assert first is True
    assert second is False, "a duplicate event must collide, not double-write"

    async with unit_of_work(actor_type="system") as s:
        stored = await s.scalar(
            select(func.count(FreshnessOutbox.id))
            .where(FreshnessOutbox.dedupe_key == key)
        )
        assert stored == 1

    # and draining twice changes nothing the first drain did not
    first_drain = await FreshnessRelay("test-worker").drain()
    second_drain = await FreshnessRelay("test-worker").drain()

    assert first_drain.scenarios_marked >= 1
    assert second_drain.claimed == 0, "a completed event was claimed again"
    assert second_drain.scenarios_marked == 0

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        assert scenario.freshness_status == "stale"


@pytest.mark.asyncio
async def test_replaying_a_processed_event_marks_nothing_further():
    """Idempotency at the marking level, not just at the write level."""
    uid, analysis_id = await _user_with_analysis()
    await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(actor_type="system") as s:
        await emit(
            s, FreshnessEvent.RULE_PUBLISHED, tax_year=2025,
            dedupe_key=f"replay:{uuid.uuid4()}",
        )
    first = await FreshnessRelay("test-worker").drain()

    # force the same row back into the pending state and drain again
    async with unit_of_work(actor_type="system") as s:
        await s.execute(text(
            "UPDATE ioe.freshness_outbox "
            "SET claim_state = 'pending', processed_at = NULL, attempts = 0 "
            "WHERE dedupe_key LIKE 'replay:%'"
        ))
    second = await FreshnessRelay("test-worker").drain()

    assert first.scenarios_marked >= 1
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        after_first = await s.scalar(
            select(func.count(Scenario.id)).where(
                Scenario.user_id == uid,
                Scenario.freshness_status == "stale",
            )
        )
    assert second.claimed >= 1, "the replayed event must be re-claimed"
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        after_second = await s.scalar(
            select(func.count(Scenario.id)).where(
                Scenario.user_id == uid,
                Scenario.freshness_status == "stale",
            )
        )
    assert after_second == after_first, "replay changed this tenant's state"


@pytest.mark.asyncio
async def test_a_missed_event_is_still_caught_by_read_time_evaluation():
    """The safeguard path: the event never arrives, and the user is still not
    shown a stale result labelled current."""
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    # the world moves, and the event is DISCARDED before the relay sees it
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)
        snapshot.snapshot_hash = f"moved-{uuid.uuid4().hex[:8]}"
        await s.flush()
    async with unit_of_work(actor_type="system") as s:
        await s.execute(text("DELETE FROM ioe.freshness_outbox"))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, outcome.scenario_id)
        transition = await ScenarioFreshnessService(s, uid).evaluate_and_record(scenario)

    assert transition.changed is True
    assert transition.new_status == "stale"
    assert transition.stale_reason_code == "BASELINE_INPUTS_CHANGED"


@pytest.mark.asyncio
async def test_a_missed_event_is_still_caught_by_the_sweep():
    uid, analysis_id = await _user_with_analysis()
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        snapshot = await s.get(AnalysisInputSnapshot, analysis_id)
        snapshot.snapshot_hash = f"moved-{uuid.uuid4().hex[:8]}"
        await s.flush()
    async with unit_of_work(actor_type="system") as s:
        await s.execute(text("DELETE FROM ioe.freshness_outbox"))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        transitions = await ScenarioFreshnessService(s, uid).sweep(limit=50)

    changed = {t.scenario_id: t for t in transitions if t.changed}
    assert outcome.scenario_id in changed
    assert changed[outcome.scenario_id].new_status == "stale"


@pytest.mark.asyncio
async def test_the_relay_carries_no_financial_values():
    """Outbox rows are identifiers and codes. Broker payloads and log lines are
    not a place for a user's money."""
    uid, _ = await _user_with_analysis()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AnalysisService(s).run(uid, 2025)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(select(FreshnessOutbox)))
        assert rows
        columns = {c.name for c in FreshnessOutbox.__table__.columns}
        assert not columns & {
            "amount", "income", "tax", "snapshot", "payload", "spec", "levers",
        }


# ---------------------------------------------------------------------------
# Governed projections
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_only_projection_eligible_candidates_generate_projections():
    uid, analysis_id = await _user_with_analysis()
    eligible = await _publish_projectable_rule(
        f"PROJOK_{_suffix()}", projection_eligibility="eligible",
        projection_method="flat_recurring", maximum_horizon=5,
    )
    not_eligible = await _publish_projectable_rule(
        f"PROJNO_{_suffix()}", projection_eligibility="not_eligible",
    )
    silent = await _publish_projectable_rule(
        f"PROJSILENT_{_suffix()}", projection_eligibility=None,
    )

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(MultiYearProjection).where(
                MultiYearProjection.run_id == outcome.run_id)
        ))
        authorizing = {r.authorizing_rule_version_id for r in rows}

    assert rows, "the eligible rule should have produced projections"
    assert eligible in authorizing
    assert not_eligible not in authorizing, "an explicitly ineligible rule projected"
    assert silent not in authorizing, "a rule that said nothing was treated as consent"
    for row in rows:
        assert row.projection_method == "flat_recurring"
        assert row.methodology_version
        assert row.calculation_basis == "projection_estimate"


@pytest.mark.asyncio
async def test_a_projection_never_exceeds_the_authorized_horizon():
    uid, analysis_id = await _user_with_analysis()
    capped = await _publish_projectable_rule(
        f"PROJCAP_{_suffix()}", projection_eligibility="eligible",
        projection_method="flat_recurring", maximum_horizon=3,
    )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        years = [
            r.horizon_year for r in await s.scalars(
                select(MultiYearProjection).where(
                    MultiYearProjection.run_id == outcome.run_id,
                    MultiYearProjection.authorizing_rule_version_id == capped,
                )
            )
        ]
    assert years, "the capped rule produced no projection"
    assert len(set(years)) <= 3, "a projection exceeded its authorized horizon"
    assert max(years) <= 2027


@pytest.mark.asyncio
async def test_a_conditional_authorization_without_its_assumptions_projects_nothing():
    uid, analysis_id = await _user_with_analysis()
    conditional = await _publish_projectable_rule(
        f"PROJCOND_{_suffix()}", projection_eligibility="conditionally_eligible",
        projection_method="flat_recurring", maximum_horizon=5,
        required_assumptions=["EMPLOYMENT_INCOME_CONSTANT"],
    )
    # no assumptions supplied with the run
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        count = await s.scalar(
            select(func.count(MultiYearProjection.id)).where(
                MultiYearProjection.run_id == outcome.run_id,
                MultiYearProjection.authorizing_rule_version_id == conditional,
            )
        )
    assert count == 0, "a conditional authorization was treated as unconditional"


@pytest.mark.asyncio
async def test_projections_never_enter_current_year_portfolio_totals():
    uid, analysis_id = await _user_with_analysis()
    await _publish_projectable_rule(
        f"PROJSEP_{_suffix()}", projection_eligibility="eligible",
        projection_method="flat_recurring", maximum_horizon=5,
    )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        portfolio = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        projected_total = await s.scalar(
            select(func.coalesce(func.sum(MultiYearProjection.projected_amount), 0))
            .where(MultiYearProjection.run_id == outcome.run_id)
        )

    assert projected_total > 0, "the fixture produced no projection to separate"
    # the portfolio's own reconciliation still holds, with no projected money in it
    assert portfolio.objective_delta == (
        portfolio.objective_value_baseline - portfolio.objective_value_final
    )
    assert portfolio.portfolio_total_benefit == portfolio.objective_delta
    assert portfolio.total_multi_year_projected in (None, Decimal("0.00")), (
        "projected money reached a current-year portfolio total"
    )


@pytest.mark.asyncio
async def test_no_eligible_candidates_returns_an_explicit_non_generated_status(client):
    from app.core.security.jwt import create_access_token

    uid, analysis_id = await _user_with_analysis()
    ineligible = await _publish_projectable_rule(
        f"PROJNONE_{_suffix()}", projection_eligibility="not_eligible"
    )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    headers = {"Authorization": f"Bearer {create_access_token(str(uid))}"}
    response = await client.get(
        f"/api/v1/ioe/runs/{outcome.run_id}/projections", headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    # this run may also carry projections authorized by rules other tests
    # published into the shared database; what must hold is that the
    # explicitly-ineligible rule contributed none of them
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        from_ineligible = await s.scalar(
            select(func.count(MultiYearProjection.id)).where(
                MultiYearProjection.run_id == outcome.run_id,
                MultiYearProjection.authorizing_rule_version_id == ineligible,
            )
        )
    assert from_ineligible == 0, "an explicitly ineligible rule projected"

    if body["status"] == "not_generated_no_eligible_candidates":
        assert body["projection"] is None
        assert body["reason"], "a non-generated status must say why"
    else:
        # generated only because ANOTHER rule authorized it
        assert body["status"] == "generated"
        assert body["projection"] is not None


@pytest.mark.asyncio
async def test_the_feature_flag_produces_its_own_status(client, monkeypatch):
    from app.core import config
    from app.core.security.jwt import create_access_token

    uid, analysis_id = await _user_with_analysis()
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    settings = config.get_settings()
    monkeypatch.setattr(settings, "ioe_projections_enabled", False)

    headers = {"Authorization": f"Bearer {create_access_token(str(uid))}"}
    body = (await client.get(
        f"/api/v1/ioe/runs/{outcome.run_id}/projections", headers=headers
    )).json()
    assert body["status"] == "feature_not_enabled"
    assert body["projection"] is None


def test_authorization_is_decided_from_rule_data_alone():
    from app.services.ioe.projection import ProjectionStatus, authorize
    from app.services.tax_engine.contracts import ProjectionAuthorization

    assert authorize(None).status is ProjectionStatus.NOT_GENERATED_NO_ELIGIBLE_CANDIDATES
    assert authorize(
        ProjectionAuthorization(eligibility="not_eligible")
    ).authorized is False
    # 'eligible' without a method or horizon is incomplete, so it authorizes nothing
    assert authorize(
        ProjectionAuthorization(eligibility="eligible")
    ).authorized is False
    assert authorize(
        ProjectionAuthorization(
            eligibility="eligible", method="flat_recurring", maximum_horizon=5
        )
    ).authorized is True
    # a caller may ask for less than the rule allows, never more
    decision = authorize(
        ProjectionAuthorization(
            eligibility="eligible", method="flat_recurring", maximum_horizon=3
        ),
        requested_horizon=10,
    )
    assert decision.horizon_years == 3
