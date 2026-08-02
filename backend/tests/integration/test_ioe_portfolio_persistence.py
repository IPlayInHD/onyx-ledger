"""P4 — portfolio assembly persisted through the orchestrator's TX-2.

What these tests are for: proving that the portfolio a user is shown is the one
the engine actually produced, and that every claim on it is reconstructible from
stored rows — the pinned objective and its three values, the ordered members,
the resource ledger, the full evaluation trace, and every excluded candidate
with a structured reason.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    OptimizationCandidate,
    OptimizationRun,
    PortfolioEvaluationStep,
    PortfolioExclusion,
    PortfolioMember,
    ResourceLedgerEntry,
    RuleAction,
    RuleOutcome,
    RuleSharedResource,
    StrategyPortfolio,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import savings as savings_domain
from app.services.ioe.orchestrator import OptimizationOrchestrator


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _user_with_income(employment: str = "95000") -> tuple[uuid.UUID, uuid.UUID]:
    """A user with real employment income, so a deduction actually moves tax."""
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"p4_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        employment_type = await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment")
        )
        employment_type_id = employment_type.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=employment_type_id,
            amount=Decimal(employment), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot={"province": "ON"}, snapshot_hash="snap-p4",
        ))
        await s.flush()
        return uid, run.id


async def _publish_lever_rule(
    code: str,
    *,
    lever_code: str,
    amount: str,
    cost_type: str = "liquidity_commitment",
    shared_resource_code: str | None = None,
) -> uuid.UUID:
    """A published, always-matching rule that references a lever BY CODE.

    The parameter is bound to the rule-authored action cost, so nothing about
    the hypothetical is invented by the IOE.
    """
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="P4 fixture",
            eligibility_basis_codes=["BASIS_P4"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible",
            portfolio_lever_code=lever_code,
            lever_parameters={"amount": "action.cost_amount"},
        ))
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE",
            description="Contribute", effort_rating=2,
            cost_type=cost_type, cost_amount=Decimal(amount),
        ))
        if shared_resource_code:
            s.add(RuleSharedResource(
                rule_version_id=version.id, resource_code=shared_resource_code,
            ))
        await s.flush()
        return version.id


async def _members_for(s, portfolio_id, version_ids) -> list[PortfolioMember]:
    """Members whose candidate came from one of THESE rule versions.

    Published rules accumulate across tests in a shared database, so every
    assertion here is scoped to the fixtures the test itself created.
    """
    mine = {
        row.id for row in await s.scalars(
            select(OptimizationCandidate).where(
                OptimizationCandidate.tax_rule_version_id.in_(version_ids)
            )
        )
    }
    return [
        m for m in await s.scalars(
            select(PortfolioMember)
            .where(PortfolioMember.portfolio_id == portfolio_id)
            .order_by(PortfolioMember.apply_order)
        )
        if m.candidate_id in mine
    ]


async def _exclusions_for(s, portfolio_id, version_ids, reason_code):
    mine = {
        row.id for row in await s.scalars(
            select(OptimizationCandidate).where(
                OptimizationCandidate.tax_rule_version_id.in_(version_ids)
            )
        )
    }
    return [
        x for x in await s.scalars(
            select(PortfolioExclusion).where(
                PortfolioExclusion.portfolio_id == portfolio_id,
                PortfolioExclusion.reason_code == reason_code,
            )
        )
        if x.candidate_id in mine
    ]


def _suffix() -> str:
    return uuid.uuid4().hex[:6].upper()


# ---------------------------------------------------------------------------
# The portfolio is persisted, and its headline is reconstructible
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_portfolio_is_persisted_with_its_pinned_objective_and_three_values():
    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"P4RRSP_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="8000",
    )

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
    assert outcome.workflow_status == "completed"
    assert outcome.portfolio_member_count == 1

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        assert pf is not None

        # the objective is PINNED by code and version, not implied
        assert pf.portfolio_objective_code == savings_domain.PORTFOLIO_OBJECTIVE_CODE.value
        assert pf.portfolio_objective_version == savings_domain.PORTFOLIO_OBJECTIVE_VERSION
        assert pf.objective_metric == savings_domain.PORTFOLIO_OBJECTIVE_CODE.value

        # all three values stored, and the delta is exactly what they define
        assert pf.objective_value_baseline is not None
        assert pf.objective_value_final is not None
        assert pf.objective_delta == (
            pf.objective_value_baseline - pf.objective_value_final
        )
        # a deduction against real income improves the position
        assert pf.objective_delta > 0
        assert pf.portfolio_total_benefit == pf.objective_delta

        # the headline is an ENGINE result, not a sum of recommendation amounts
        assert pf.portfolio_tax < pf.baseline_tax

        # a rule-based assembler claims nothing about optimality
        assert pf.optimality_claim == "none"
        assert pf.assembly_method == "greedy_ranked"
        assert pf.search_budget_exhausted is False
        assert pf.engine_runs_used >= 3          # baseline + standalone + combined


@pytest.mark.asyncio
async def test_members_ledger_and_trace_are_persisted_in_apply_order():
    uid, analysis_id = await _user_with_income()
    versions = [
        await _publish_lever_rule(
            f"P4MEM_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="6000",
            shared_resource_code="RRSP_ROOM",
        ),
        await _publish_lever_rule(
            f"P4FHSA_{_suffix()}", lever_code="INCREASE_FHSA_DEDUCTION", amount="4000",
            shared_resource_code="FHSA_ROOM",
        ),
    ]

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        mine = await _members_for(s, pf.id, versions)
        assert len(mine) == 2
        for m in mine:
            assert m.candidate_id is not None

        all_members = list(await s.scalars(
            select(PortfolioMember)
            .where(PortfolioMember.portfolio_id == pf.id)
            .order_by(PortfolioMember.apply_order)
        ))
        # apply_order is a dense, deterministic sequence over the whole portfolio
        assert [m.apply_order for m in all_members] == list(range(len(all_members)))

        # I-1 holds over the STORED rows, not only in memory
        assert sum(
            (m.incremental_benefit for m in all_members), Decimal(0)
        ) == pf.objective_delta

        # the ledger records every pool the levers touched
        ledger = list(await s.scalars(
            select(ResourceLedgerEntry).where(ResourceLedgerEntry.portfolio_id == pf.id)
        ))
        assert {"RRSP_ROOM", "FHSA_ROOM"} <= {e.resource_code for e in ledger}
        for entry in ledger:
            assert entry.allocated >= 0
            if entry.capacity is not None:
                assert entry.allocated <= entry.capacity

        steps = list(await s.scalars(
            select(PortfolioEvaluationStep)
            .where(PortfolioEvaluationStep.portfolio_id == pf.id)
            .order_by(PortfolioEvaluationStep.step_index)
        ))
        # one row per engine run: baseline, standalones, trials, accepts, final
        assert [x.step_index for x in steps] == list(range(len(steps)))
        assert steps[0].stage == "baseline"
        assert steps[-1].stage == "final_combined"
        assert {"standalone", "accepted"} <= {x.stage for x in steps}
        # the trace closes on the stored final value
        assert steps[-1].objective_value == pf.objective_value_final
        assert steps[-1].objective_delta == pf.objective_delta


@pytest.mark.asyncio
async def test_trace_does_not_duplicate_the_users_financial_inputs():
    """The trace explains the search. The user's money lives in the frozen
    analysis snapshot and is deliberately not copied here."""
    uid, analysis_id = await _user_with_income(employment="123456")
    await _publish_lever_rule(
        f"P4TRACE_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="5000",
    )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        steps = list(await s.scalars(
            select(PortfolioEvaluationStep)
            .where(PortfolioEvaluationStep.portfolio_id == pf.id)
        ))
        stored = {
            column.name
            for column in PortfolioEvaluationStep.__table__.columns
        }
        assert "inputs" not in stored and "snapshot" not in stored
        # no step carries the raw income figure in any text column
        for step in steps:
            assert "123456" not in (step.reason_code or "")
            assert "123456" not in step.stage


# ---------------------------------------------------------------------------
# Excluded candidates are retained with structured reasons
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_insufficient_cash_is_persisted_as_a_structured_exclusion():
    uid, analysis_id = await _user_with_income()
    versions = [await _publish_lever_rule(
        f"P4CASH_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="20000",
    )]

    outcome = await OptimizationOrchestrator(uid).generate(
        analysis_id, user_constraints={"available_cash": "500"},
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        cash = await _exclusions_for(s, pf.id, versions, "INSUFFICIENT_CASH")
        assert cash, "an excluded candidate must be retained, not dropped"
        assert cash[0].membership == "excluded_constraint"
        # the user is told what would change the answer
        assert "INCREASE_AVAILABLE_CASH" in cash[0].resolution_options
        assert cash[0].candidate_id is not None

        # and it is not in the portfolio it could not afford
        assert not await _members_for(s, pf.id, versions)


@pytest.mark.asyncio
async def test_shared_resource_exhaustion_names_the_pool_that_blocked_it():
    uid, analysis_id = await _user_with_income()
    versions = [
        await _publish_lever_rule(
            f"P4POOLA_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION",
            amount="9000", shared_resource_code="RRSP_ROOM",
        ),
        await _publish_lever_rule(
            f"P4POOLB_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION",
            amount="9000", shared_resource_code="RRSP_ROOM",
        ),
    ]

    outcome = await OptimizationOrchestrator(uid).generate(
        analysis_id,
        user_constraints={"resource_capacities": {"RRSP_ROOM": "10000"}},
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        # the pool is allocated ONCE — this is the anti-double-counting invariant
        entry = await s.scalar(
            select(ResourceLedgerEntry).where(
                ResourceLedgerEntry.portfolio_id == pf.id,
                ResourceLedgerEntry.resource_code == "RRSP_ROOM",
            )
        )
        assert entry.capacity == Decimal("10000.00")
        assert entry.allocated <= entry.capacity

        blocked = await _exclusions_for(
            s, pf.id, versions, "SHARED_RESOURCE_EXHAUSTED"
        )
        assert blocked, "the second claim on a capped pool must be excluded"
        assert blocked[0].shared_resource_code == "RRSP_ROOM"
        assert "SPLIT_ALLOCATION" in blocked[0].resolution_options


@pytest.mark.asyncio
async def test_non_evaluable_candidate_is_excluded_and_never_guessed():
    """A rule with no contract authoring has no lever, so it is reported
    informationally rather than acted on."""
    uid, analysis_id = await _user_with_income()
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"P4BARE_{_suffix()}"
        rule = TaxRule(code=code, name=code, category="credit", jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="no contract",
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template="bare",
        ))
        await s.flush()
        versions = [version.id]

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        exclusions = await _exclusions_for(
            s, pf.id, versions, "NOT_PORTFOLIO_EVALUABLE"
        )
        assert exclusions
        assert exclusions[0].membership == "excluded_not_evaluable"
        assert "COMPLETE_MISSING_DATA" in exclusions[0].resolution_options


# ---------------------------------------------------------------------------
# Immutability + isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_persisted_portfolio_evidence_is_immutable():
    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"P4IMM_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="7000",
    )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        portfolio_id = pf.id

    for table in ("portfolio_evaluation_step", "portfolio_exclusion"):
        # a fresh transaction per attempt: the first failure aborts its own
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            with pytest.raises(DBAPIError, match="immutable calculation evidence"):
                await s.execute(
                    text(f"UPDATE ioe.{table} SET reason_code = 'tampered' "  # noqa: S608
                         "WHERE portfolio_id = :p"),
                    {"p": portfolio_id},
                )
            await s.rollback()


@pytest.mark.asyncio
async def test_a_users_portfolio_is_not_visible_to_another_user():
    uid_a, analysis_a = await _user_with_income()
    uid_b, _ = await _user_with_income()
    await _publish_lever_rule(
        f"P4RLS_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="7000",
    )
    outcome = await OptimizationOrchestrator(uid_a).generate(analysis_a)

    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        assert await s.scalar(
            select(OptimizationRun).where(OptimizationRun.id == outcome.run_id)
        ) is None
        # the portfolio is reachable only through its run, which B cannot see
        assert await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        ) is None


# ---------------------------------------------------------------------------
# Per-concept totals stay separate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_commitment_concepts_are_stored_separately_not_merged():
    """A liquidity commitment is not an expenditure. Persisting them in one
    column would make a retained asset look like money the user lost."""
    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"P4SEP_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="6000",
        cost_type="liquidity_commitment",
    )
    await _publish_lever_rule(
        f"P4DON_{_suffix()}", lever_code="INCREASE_DONATIONS", amount="1000",
        cost_type="nonrecoverable_expenditure",
    )

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        members = list(await s.scalars(
            select(PortfolioMember).where(PortfolioMember.portfolio_id == pf.id)
        ))
        candidate_ids = {m.candidate_id for m in members}
        selected = [
            row for row in await s.scalars(
                select(OptimizationCandidate)
                .where(OptimizationCandidate.run_id == outcome.run_id)
            )
            if row.id in candidate_ids
        ]
        assert selected

        # each concept has its own column and none is folded into another
        assert pf.total_liquidity_commitment is not None
        assert pf.total_nonrecoverable_expenditure is not None
        assert pf.total_asset_transfer is not None
        if pf.total_liquidity_commitment > 0:
            assert pf.total_liquidity_commitment != pf.total_nonrecoverable_expenditure
