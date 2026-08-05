"""P4 closing items 4 and 6, verified against real PostgreSQL.

Item 4 — eligibility-changing portfolio actions. Re-evaluation is preferred and
happens against the SAME pinned rule-version set; where it cannot be resolved,
the candidate is excluded with an explicit `requires_re_evaluation` result. A
rule published after pinning can never enter the in-flight run.

Item 6 — the three-way reconciliation, asserted on STORED rows: the stored
objective delta, the sum of stored incremental deltas, and baseline minus final
all agree under the same rounding policy.
"""
import uuid
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    CandidateCost,
    OptimizationCandidate,
    PortfolioExclusion,
    PortfolioMember,
    StrategyPortfolio,
)
from app.database.session import unit_of_work
from app.services.ioe.domain.models import OptimizationCandidate as DomainCandidate
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.portfolio.eligibility import (
    PinnedEligibilityRechecker,
    RecheckResult,
    load_pinned_condition_trees,
)
from tests.integration.test_ioe_portfolio_persistence import (
    _publish_lever_rule,
    _suffix,
    _user_with_income,
)

MONEY = Decimal("0.01")


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


# ---------------------------------------------------------------------------
# Item 4 — eligibility re-evaluation against the pinned set
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rechecker_only_ever_sees_the_pinned_versions():
    """The rechecker holds pre-loaded trees and no session, so there is no code
    path from it back to the rules tables at all."""
    uid, analysis_id = await _user_with_income()
    pinned_version = await _publish_lever_rule(
        f"P4RE_A_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="5000",
    )

    orch = OptimizationOrchestrator(uid)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        spec = await orch._pin_specification(
            s, analysis_id, user_constraints=None, assumptions=None
        )
        trees = await load_pinned_condition_trees(s, spec.pinned_rule_version_ids)

    assert str(pinned_version) in trees

    # a NEW rule is published while the run is in flight
    intruder = await _publish_lever_rule(
        f"P4RE_B_{_suffix()}", lever_code="INCREASE_DONATIONS", amount="1000",
    )
    assert str(intruder) not in trees, "a rule published after pinning entered the set"

    rechecker = PinnedEligibilityRechecker(trees, lambda _inputs: {})
    assert rechecker.pinned_version_count == len(spec.pinned_rule_version_ids)
    assert not hasattr(rechecker, "s")

    # a candidate on the intruder version cannot be resolved — never assumed
    intruder_candidate = DomainCandidate(
        candidate_key="x", opportunity_code="x", rule_version_id=str(intruder),
        eligibility_status="eligible",
    )
    assert rechecker.check(intruder_candidate, {}) is RecheckResult.INDETERMINATE


@pytest.mark.asyncio
async def test_a_candidate_with_no_pinned_tree_is_marked_requires_re_evaluation():
    """'We could not tell' is persisted as its own state, not folded into a
    constraint exclusion that would read as 'we checked and it failed'."""
    from app.services.ioe.domain import portfolio as assembly
    from app.services.ioe.domain.enums import PortfolioMembership

    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"P4REQ_A_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="5000",
    )
    await _publish_lever_rule(
        f"P4REQ_B_{_suffix()}", lever_code="INCREASE_FHSA_DEDUCTION", amount="4000",
    )

    orch = OptimizationOrchestrator(uid)
    original_evaluate = assembly.assemble

    def assemble_with_empty_trees(ranked, relationships, baseline, evaluate, cons):
        # an empty pinned tree set: nothing can be re-resolved
        blind = PinnedEligibilityRechecker({}, lambda _inputs: {})
        cons.eligibility_recheck = blind.check
        return original_evaluate(ranked, relationships, baseline, evaluate, cons)

    assembly_module = assembly
    assembly_module.assemble = assemble_with_empty_trees
    try:
        outcome = await orch.generate(analysis_id)
    finally:
        assembly_module.assemble = original_evaluate

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = list(await s.scalars(
            select(OptimizationCandidate).where(
                OptimizationCandidate.run_id == outcome.run_id,
                OptimizationCandidate.requires_re_evaluation.is_(True),
            )
        ))
        assert rows, "an unresolvable candidate must be flagged, not silently kept"
        for row in rows:
            assert row.portfolio_membership == (
                PortfolioMembership.REQUIRES_RE_EVALUATION.value
            )
            assert row.re_evaluation_reason_code == "REQUIRES_RE_EVALUATION"

        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        flagged = {r.id for r in rows}
        exclusions = list(await s.scalars(
            select(PortfolioExclusion).where(
                PortfolioExclusion.portfolio_id == pf.id,
                PortfolioExclusion.reason_code == "REQUIRES_RE_EVALUATION",
            )
        ))
        assert {x.candidate_id for x in exclusions} & flagged
        assert "RE_RUN_OPTIMIZATION" in exclusions[0].resolution_options
        # and it is not acted on
        members = {
            m.candidate_id for m in await s.scalars(
                select(PortfolioMember).where(PortfolioMember.portfolio_id == pf.id)
            )
        }
        assert not (members & flagged)


@pytest.mark.asyncio
async def test_re_evaluation_uses_the_same_facts_the_first_evaluation_used():
    """Re-evaluation and first evaluation share one fact function, so they agree
    by construction rather than by coincidence."""
    from app.services.ioe.portfolio.eligibility import engine_facts_for
    from app.services.ioe.portfolio.service import inputs_from
    from app.services.tax_engine.service import TaxEngineService

    uid, _ = await _user_with_income(employment="88000")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        engine = TaxEngineService(s)
        inp = await engine.build_input_from_live_sources(uid, 2025)
        first = engine.facts(inp, engine.run(inp))

    assert engine_facts_for(inputs_from(inp)) == first


@pytest.mark.asyncio
async def test_a_lower_net_income_after_an_action_changes_what_rules_match():
    """The situation item 4 exists for: an earlier action moves a gated fact."""
    from app.services.ioe.portfolio.eligibility import engine_facts_for
    from app.services.ioe.portfolio.service import inputs_from
    from app.services.tax_engine.service import TaxEngineService

    uid, _ = await _user_with_income(employment="95000")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        inp = await TaxEngineService(s).build_input_from_live_sources(uid, 2025)

    before = engine_facts_for(inputs_from(inp))
    after_inputs = dict(inputs_from(inp))
    after_inputs["rrsp_deduction"] = Decimal("20000")
    after = engine_facts_for(after_inputs)

    assert after["income.net"] < before["income.net"], (
        "the fixture must actually move the gated fact for this to mean anything"
    )
    # a rule gated on net income below a threshold would flip between these two
    tree = {
        "logical_op": "AND", "groups": [],
        "conditions": [{
            "fact_key": "income.net", "operator": "lt", "value_type": "number",
            "value_number": before["income.net"] - Decimal(1),
            "value_number_high": None, "value_text": None,
            "value_boolean": None, "value_set": None,
        }],
    }
    version = str(uuid.uuid4())
    rechecker = PinnedEligibilityRechecker({version: tree}, engine_facts_for)
    candidate = DomainCandidate(
        candidate_key="k", opportunity_code="k", rule_version_id=version,
        eligibility_status="eligible",
    )
    assert rechecker.check(candidate, inputs_from(inp)) is RecheckResult.INELIGIBLE
    assert rechecker.check(candidate, after_inputs) is RecheckResult.ELIGIBLE


# ---------------------------------------------------------------------------
# Item 6 — three-way reconciliation on stored rows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stored_objective_delta_reconciles_three_ways():
    """delta == Σ incremental == baseline − final, all quantized identically.

    Read back from the database rather than asserted in memory: a headline that
    only reconciles before it is written is not a headline anyone can audit.
    """
    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"P4REC_A_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="7000",
    )
    await _publish_lever_rule(
        f"P4REC_B_{_suffix()}", lever_code="INCREASE_FHSA_DEDUCTION", amount="3000",
    )

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
        members = list(await s.scalars(
            select(PortfolioMember)
            .where(PortfolioMember.portfolio_id == pf.id)
            .order_by(PortfolioMember.apply_order)
        ))

    stored_delta = pf.objective_delta.quantize(MONEY, ROUND_HALF_UP)
    incremental_sum = sum(
        (m.incremental_benefit for m in members), Decimal(0)
    ).quantize(MONEY, ROUND_HALF_UP)
    baseline_minus_final = (
        pf.objective_value_baseline - pf.objective_value_final
    ).quantize(MONEY, ROUND_HALF_UP)

    assert stored_delta == incremental_sum == baseline_minus_final
    # the same figure the user is shown
    assert pf.portfolio_total_benefit.quantize(MONEY, ROUND_HALF_UP) == stored_delta
    # incremental deltas are in exact apply order, densely numbered
    assert [m.apply_order for m in members] == list(range(len(members)))


@pytest.mark.asyncio
async def test_no_inequality_is_imposed_between_total_and_sum_of_standalone():
    """Interaction may be positive or negative; neither is treated as an error."""
    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"P4INT_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="6000",
    )
    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        pf = await s.scalar(
            select(StrategyPortfolio).where(StrategyPortfolio.run_id == outcome.run_id)
        )
    assert pf.sum_of_standalone is not None
    assert pf.interaction_delta == (
        pf.sum_of_standalone - pf.objective_delta
    ).quantize(MONEY, ROUND_HALF_UP)
    assert pf.additivity_class in ("additive", "sub_additive", "super_additive")


# ---------------------------------------------------------------------------
# Item 3 — the resolution is persisted, not just computed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cost_normalization_provenance_is_persisted():
    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"P4CST_R_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="5000",
        cost_type="required_cash_contribution",
    )
    await _publish_lever_rule(
        f"P4CST_D_{_suffix()}", lever_code="INCREASE_DONATIONS", amount="800",
        cost_type="required_cash_contribution",
    )

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        candidate_ids = {
            row.id for row in await s.scalars(
                select(OptimizationCandidate).where(
                    OptimizationCandidate.run_id == outcome.run_id
                )
            )
        }
        costs = [
            row for row in await s.scalars(select(CandidateCost))
            if row.candidate_id in candidate_ids
        ]

    assert costs, "candidate costs must be persisted, not held only in memory"
    resolved = {c.cost_type for c in costs if c.cost_type_source == "lever_registry"}
    # the same authored value resolved to two different economic facts
    assert {"asset_transfer", "nonrecoverable_expenditure"} <= resolved, resolved
    for cost in costs:
        assert cost.authored_cost_type is not None
        assert cost.taxonomy_version == "1.0.0"
        if cost.cost_type_source != "authored_verbatim":
            # a derivation always records what the rule actually said
            assert cost.authored_cost_type == "required_cash_contribution"
