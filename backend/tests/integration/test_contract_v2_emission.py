"""RulesEvaluatorService emits Opportunity contract v2 from real rule data.

Proves the P0 boundary guarantee: every legally meaningful field the IOE will
consume is SUPPLIED by the rules layer, and a version without contract-v2
authoring is reported `indeterminate` (so the IOE excludes it) rather than
having its legal facts inferred.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    Jurisdiction,
    RuleAction,
    RuleDeadline,
    RuleDependency,
    RuleOutcome,
    RuleRequiredDocument,
    RuleSharedResource,
    TaxRule,
    TaxRuleVersion,
)
from app.database.session import unit_of_work
from app.services.tax_engine.rules_service import RulesEvaluatorService


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _published_rule(s, code: str, *, with_contract: bool) -> TaxRuleVersion:
    """A published rule with no condition gate (always matches), optionally
    carrying contract-v2 authoring."""
    jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
    rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                   jurisdiction_id=jur.id)
    s.add(rule)
    await s.flush()

    version = TaxRuleVersion(
        tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
        expiry_date=date(2025, 12, 31), status="published",
        description="Contract emission fixture", source_url="https://canada.ca/x",
        eligibility_basis_codes=["AGE_71_UNDER", "HAS_EARNED_INCOME"] if with_contract else None,
    )
    s.add(version)
    await s.flush()

    outcome = RuleOutcome(
        rule_version_id=version.id, outcome_type="recommend", priority=1,
        title_template=f"{code} opportunity", mechanism="deduction",
        why_template="Because the rule applies.",
    )
    if with_contract:
        outcome.economic_effect_type = "current_year_tax_reduction"
        outcome.reversibility = "irreversible"
        outcome.portfolio_lever_code = "INCREASE_RRSP_DEDUCTION"
        outcome.lever_parameters = {"amount": "rule.max_amount"}
    s.add(outcome)

    if with_contract:
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE_RRSP",
            description="Contribute before the deadline", effort_rating=2,
            cost_type="required_cash_contribution", cost_amount=Decimal("5000"),
            deadline_code="RRSP_DEADLINE", sort_order=0,
        ))
        s.add(RuleRequiredDocument(
            rule_version_id=version.id, document_type_code="RRSP", necessity="required",
        ))
        s.add(RuleDependency(
            rule_version_id=version.id, depends_on_rule_code="SOME_OTHER_RULE",
            dependency_type="requires",
        ))
        s.add(RuleDeadline(
            rule_version_id=version.id, deadline_code="RRSP_DEADLINE",
            deadline_date=date(2026, 3, 2), is_hard=True, jurisdiction_code="FED",
        ))
        s.add(RuleSharedResource(
            rule_version_id=version.id, resource_code="RRSP_ROOM", pool_scope="individual",
        ))
    await s.flush()
    return version


@pytest.mark.asyncio
async def test_contract_fields_are_supplied_by_the_rules_layer():
    code = f"CV2_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        version = await _published_rule(s, code, with_contract=True)

        opps = await RulesEvaluatorService(s).evaluate(2025, {})
        mine = [o for o in opps if o.rule_version_id == version.id]
        assert len(mine) == 1
        o = mine[0]

        assert o.contract_version == "opportunity/2.0.0"
        assert o.jurisdiction == "FED"
        assert o.tax_year == 2025

        # legal determinations came from rule DATA, verbatim
        assert o.eligibility_status == "eligible"
        assert o.eligibility_basis_codes == ("AGE_71_UNDER", "HAS_EARNED_INCOME")
        assert [a.action_code for a in o.required_actions] == ["CONTRIBUTE_RRSP"]
        assert o.required_actions[0].cost_type == "required_cash_contribution"
        assert o.required_actions[0].cost_amount == Decimal("5000")
        assert [d.document_type_code for d in o.required_documents] == ["RRSP"]
        assert [d.dependency_type for d in o.dependencies] == ["requires"]
        assert o.applicable_deadlines[0].deadline_date == date(2026, 3, 2)
        assert o.applicable_deadlines[0].is_hard is True
        assert o.expiry_information.is_expiring is True
        assert o.expiry_information.expiry_date == date(2025, 12, 31)

        # economics supplied, not inferred
        assert o.economic_effect_type == "current_year_tax_reduction"
        assert o.reversibility == "irreversible"
        assert o.shared_resource_codes == ("RRSP_ROOM",)

        # lever referenced BY CODE — no engine field named in rule data
        assert o.portfolio_lever_ref.lever_code == "INCREASE_RRSP_DEDUCTION"
        assert o.portfolio_lever_ref.parameter_bindings == {"amount": "rule.max_amount"}
        assert o.is_portfolio_evaluable


@pytest.mark.asyncio
async def test_version_without_contract_authoring_is_indeterminate_and_not_evaluable():
    code = f"CV2NONE_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        version = await _published_rule(s, code, with_contract=False)

        opps = await RulesEvaluatorService(s).evaluate(2025, {})
        o = next(o for o in opps if o.rule_version_id == version.id)

        # absence is explicit — nothing is invented to fill the gap
        assert o.eligibility_status == "indeterminate"
        assert o.eligibility_basis_codes == ()
        assert o.required_actions == ()
        assert o.required_documents == ()
        assert o.dependencies == ()
        assert o.applicable_deadlines == ()
        assert o.shared_resource_codes == ()
        assert o.economic_effect_type is None
        assert o.portfolio_lever_ref is None
        assert not o.has_contract_metadata
        assert not o.is_portfolio_evaluable      # the IOE must exclude it


@pytest.mark.asyncio
async def test_conditional_document_yields_conditionally_eligible():
    code = f"CV2COND_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        version = await _published_rule(s, code, with_contract=True)
        s.add(RuleRequiredDocument(
            rule_version_id=version.id, document_type_code="MEDICAL", necessity="conditional",
        ))
        await s.flush()

        opps = await RulesEvaluatorService(s).evaluate(2025, {})
        o = next(o for o in opps if o.rule_version_id == version.id)
        assert o.eligibility_status == "conditionally_eligible"
        assert o.is_portfolio_evaluable      # conditional still participates


@pytest.mark.asyncio
async def test_existing_seeded_rule_still_evaluates_with_v1_fields():
    """Back-compat: the seeded medical credit still produces its v1 impact."""
    async with unit_of_work(actor_type="admin") as s:
        opps = await RulesEvaluatorService(s).evaluate(
            2025, {"expense.medical.total": Decimal("3000"),
                   "income.net": Decimal("75000")}
        )
        med = [o for o in opps if "medical" in o.opportunity_code]
        assert med, "seeded medical rule should still match"
        # unchanged v1 arithmetic: (3000 - min(75000*0.03, 2834)) * 0.145 = 108.75
        assert med[0].calculated_impact == Decimal("108.75")
        # v1 alias and v2 canonical name agree
        assert med[0].estimated_impact == med[0].calculated_impact
        assert med[0].calculation_basis == "rule_formula_determined"
        # the seeded rule predates contract v2, so it is correctly indeterminate
        assert med[0].eligibility_status == "indeterminate"
        assert not med[0].is_portfolio_evaluable
