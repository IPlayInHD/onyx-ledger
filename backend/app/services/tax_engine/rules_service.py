"""DB-backed rules evaluator — reads PUBLISHED tax_rule_versions for the year,
evaluates their stored condition trees against the fact map, and computes each
matched rule's impact via its stored calc_formula (sandboxed RPN). Emits
structured opportunities that cite the exact rule version. No AI.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    CalcFormula,
    CalcFormulaInput,
    RuleCondition,
    RuleConditionGroup,
    RuleOutcome,
    TaxRule,
    TaxRuleVersion,
)
from app.services.tax_engine.core.condition_eval import eval_group
from app.services.tax_engine.core.formula_sandbox import evaluate_rpn


@dataclass
class Opportunity:
    rule_version_id: object
    opportunity_code: str
    title: str
    category: str
    mechanism: str | None
    where_text: str | None
    how_text: str | None
    why_text: str | None
    estimated_impact: Decimal | None
    priority: int
    citation: str | None


class RulesEvaluatorService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def evaluate(self, tax_year: int, facts: dict) -> list[Opportunity]:
        versions = list(await self.s.scalars(
            select(TaxRuleVersion).where(
                TaxRuleVersion.tax_year == tax_year,
                TaxRuleVersion.status == "published",
            )
        ))
        opportunities: list[Opportunity] = []
        for v in versions:
            tree = await self._load_condition_tree(v.id)
            if tree is not None and not eval_group(tree, facts):
                continue
            rule = await self.s.get(TaxRule, v.tax_rule_id)
            for outcome in await self.s.scalars(
                select(RuleOutcome).where(RuleOutcome.rule_version_id == v.id)
            ):
                impact = await self._impact(outcome.impact_formula_id, facts)
                opportunities.append(Opportunity(
                    rule_version_id=v.id,
                    opportunity_code=(rule.code.lower() if rule else "opportunity"),
                    title=outcome.title_template or (rule.name if rule else "Opportunity"),
                    category=(rule.category if rule else "credit"),
                    mechanism=outcome.mechanism,
                    where_text=outcome.where_template,
                    how_text=outcome.how_template,
                    why_text=outcome.why_template,
                    estimated_impact=impact,
                    priority=outcome.priority,
                    citation=v.source_url,
                ))
        return opportunities

    async def _load_condition_tree(self, version_id) -> dict | None:
        groups = list(await self.s.scalars(
            select(RuleConditionGroup).where(RuleConditionGroup.rule_version_id == version_id)
        ))
        if not groups:
            return None
        by_id = {g.id: {"logical_op": g.logical_op, "conditions": [], "groups": [],
                        "_parent": g.parent_group_id} for g in groups}
        for g in groups:
            for c in await self.s.scalars(
                select(RuleCondition).where(RuleCondition.group_id == g.id)
            ):
                by_id[g.id]["conditions"].append({
                    "fact_key": c.fact_key, "operator": c.operator,
                    "value_type": c.value_type, "value_number": c.value_number,
                    "value_number_high": c.value_number_high, "value_text": c.value_text,
                    "value_boolean": c.value_boolean, "value_set": None,
                })
        root = None
        for gid, node in by_id.items():
            parent = node.pop("_parent")
            if parent is None:
                root = node
            else:
                by_id[parent]["groups"].append(node)
        return root

    async def _impact(self, formula_id, facts: dict) -> Decimal | None:
        if not formula_id:
            return None
        formula = await self.s.get(CalcFormula, formula_id)
        if not formula:
            return None
        variables: dict[str, Decimal] = {}
        for fi in await self.s.scalars(
            select(CalcFormulaInput).where(CalcFormulaInput.formula_id == formula_id)
        ):
            if fi.fact_key is not None and fi.fact_key in facts and facts[fi.fact_key] is not None:
                variables[fi.param_name] = Decimal(str(facts[fi.fact_key]))
            elif fi.literal_value is not None:
                variables[fi.param_name] = Decimal(str(fi.literal_value))
            else:
                variables[fi.param_name] = Decimal(0)
        try:
            return evaluate_rpn(formula.expression, variables).quantize(Decimal("0.01"))
        except Exception:  # noqa: BLE001
            return None
