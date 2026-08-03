"""DB-backed rules evaluator — the eligibility and legislation-interpretation authority.

Reads PUBLISHED tax_rule_versions for the year, evaluates their stored condition
trees against the fact map, computes each matched rule's impact via its stored
calc_formula (sandboxed RPN), and emits an `OpportunityContractV2` per matched
outcome. No AI.

Contract v2 (see app/services/tax_engine/contracts.py): this service supplies
every legally meaningful field — eligibility status and basis codes, required
actions and documents, dependencies, deadlines, citations, expiry, economic
classification — as RULE DATA loaded from the rules schema. Downstream consumers
(notably the IOE) use them verbatim; they may not infer them from rule prose.

Absence is meaningful: a version with no `eligibility_basis_codes` carries no
contract-v2 authoring and is reported `eligibility_status='indeterminate'` so
consumers exclude it rather than guessing.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    CalcFormula,
    CalcFormulaInput,
    Jurisdiction,
    LegislationReference,
    RuleAction,
    RuleCondition,
    RuleConditionGroup,
    RuleDeadline,
    RuleDependency,
    RuleOutcome,
    RuleRequiredDocument,
    RuleSharedResource,
    TaxRule,
    TaxRuleVersion,
)
from app.services.tax_engine.contracts import (
    ActionSpec,
    CitationSpec,
    DeadlineSpec,
    DependencySpec,
    DocumentSpec,
    ExpiryInfo,
    Opportunity,
    OpportunityContractV2,
    PortfolioLeverRef,
    ProjectionAuthorization,
)
from app.services.tax_engine.core.condition_eval import eval_group
from app.services.tax_engine.core.formula_sandbox import evaluate_rpn

__all__ = ["Opportunity", "OpportunityContractV2", "RulesEvaluatorService"]


class RulesEvaluatorService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def evaluate(
        self,
        tax_year: int,
        facts: dict,
        *,
        pinned_rule_version_ids: Collection | None = None,
    ) -> list[OpportunityContractV2]:
        """Evaluate published rules for the year against the fact map.

        `pinned_rule_version_ids` CONSTRAINS the evaluation to an exact,
        immutable version set. When supplied, only those versions are considered
        — a rule published after the set was pinned cannot enter the result, so
        an in-flight optimization keeps evaluating the snapshot it started with.
        Passing an empty collection means "no rules pinned" and yields nothing;
        that is distinct from passing None, which means "resolve now".
        """
        stmt = select(TaxRuleVersion).where(
            TaxRuleVersion.tax_year == tax_year,
            TaxRuleVersion.status == "published",
        )
        if pinned_rule_version_ids is not None:
            pinned = list(pinned_rule_version_ids)
            if not pinned:
                return []
            stmt = stmt.where(TaxRuleVersion.id.in_(pinned))
        versions = list(await self.s.scalars(stmt))
        if not versions:
            return []

        # Evaluate gates first, then load contract data only for matched versions.
        matched: list[TaxRuleVersion] = []
        for v in versions:
            tree = await self._load_condition_tree(v.id)
            if tree is not None and not eval_group(tree, facts):
                continue
            matched.append(v)
        if not matched:
            return []

        contract = await self._load_contract_data([v.id for v in matched])
        jurisdictions = await self._load_jurisdictions(matched)

        opportunities: list[OpportunityContractV2] = []
        for v in matched:
            rule = await self.s.get(TaxRule, v.tax_rule_id)
            basis_codes = tuple(v.eligibility_basis_codes or ())
            documents = tuple(contract["documents"].get(v.id, ()))
            citations = await self._citations(v)

            for outcome in await self.s.scalars(
                select(RuleOutcome).where(RuleOutcome.rule_version_id == v.id)
            ):
                impact = await self._impact(outcome.impact_formula_id, facts)
                opportunities.append(OpportunityContractV2(
                    rule_version_id=v.id,
                    opportunity_code=(rule.code.lower() if rule else "opportunity"),
                    title=outcome.title_template or (rule.name if rule else "Opportunity"),
                    category=(rule.category if rule else "credit"),
                    jurisdiction=jurisdictions.get(v.tax_rule_id),
                    tax_year=v.tax_year,
                    mechanism=outcome.mechanism,
                    where_text=outcome.where_template,
                    how_text=outcome.how_template,
                    why_text=outcome.why_template,
                    priority=outcome.priority,
                    citation=v.source_url,
                    # ---- legal determinations (rules-supplied) ----
                    eligibility_status=_eligibility_status(basis_codes, documents),
                    eligibility_basis_codes=basis_codes,
                    required_actions=tuple(contract["actions"].get(v.id, ())),
                    required_documents=documents,
                    dependencies=tuple(contract["dependencies"].get(v.id, ())),
                    applicable_deadlines=tuple(contract["deadlines"].get(v.id, ())),
                    citations=citations,
                    expiry_information=_expiry(v),
                    # `assumptions_required` has no rule-data source yet; it stays
                    # empty rather than being synthesized.
                    # ---- economics (rules-supplied) ----
                    calculation_basis=(
                        "rule_formula_determined"
                        if impact is not None and outcome.impact_formula_id else None
                    ),
                    calculated_impact=impact,
                    economic_effect_type=outcome.economic_effect_type,
                    reversibility=outcome.reversibility,
                    shared_resource_codes=tuple(contract["resources"].get(v.id, ())),
                    portfolio_lever_ref=_lever_ref(outcome),
                    projection=_projection_authorization(outcome),
                ))
        return opportunities

    # ---- contract-v2 data loading (batched per evaluation) ------------------
    async def _load_contract_data(self, version_ids: list) -> dict[str, dict]:
        actions: dict[object, list[ActionSpec]] = defaultdict(list)
        for a in await self.s.scalars(
            select(RuleAction)
            .where(RuleAction.rule_version_id.in_(version_ids))
            .order_by(RuleAction.sort_order)
        ):
            actions[a.rule_version_id].append(ActionSpec(
                action_code=a.action_code, description=a.description,
                effort_rating=a.effort_rating, cost_type=a.cost_type,
                cost_amount=a.cost_amount, deadline_code=a.deadline_code,
            ))

        documents: dict[object, list[DocumentSpec]] = defaultdict(list)
        for d in await self.s.scalars(
            select(RuleRequiredDocument).where(
                RuleRequiredDocument.rule_version_id.in_(version_ids))
        ):
            documents[d.rule_version_id].append(DocumentSpec(
                document_type_code=d.document_type_code, necessity=d.necessity, note=d.note,
            ))

        dependencies: dict[object, list[DependencySpec]] = defaultdict(list)
        for dep in await self.s.scalars(
            select(RuleDependency).where(RuleDependency.rule_version_id.in_(version_ids))
        ):
            dependencies[dep.rule_version_id].append(DependencySpec(
                depends_on_rule_code=dep.depends_on_rule_code,
                dependency_type=dep.dependency_type, note=dep.note,
            ))

        deadlines: dict[object, list[DeadlineSpec]] = defaultdict(list)
        for dl in await self.s.scalars(
            select(RuleDeadline).where(RuleDeadline.rule_version_id.in_(version_ids))
        ):
            deadlines[dl.rule_version_id].append(DeadlineSpec(
                deadline_code=dl.deadline_code, deadline_date=dl.deadline_date,
                description=dl.description, is_hard=dl.is_hard,
                jurisdiction_code=dl.jurisdiction_code,
            ))

        resources: dict[object, list[str]] = defaultdict(list)
        for r in await self.s.scalars(
            select(RuleSharedResource).where(RuleSharedResource.rule_version_id.in_(version_ids))
        ):
            resources[r.rule_version_id].append(r.resource_code)

        return {"actions": actions, "documents": documents, "dependencies": dependencies,
                "deadlines": deadlines, "resources": resources}

    async def _load_jurisdictions(self, versions: list[TaxRuleVersion]) -> dict[object, str]:
        rule_ids = {v.tax_rule_id for v in versions}
        if not rule_ids:
            return {}
        rows = await self.s.execute(
            select(TaxRule.id, Jurisdiction.code)
            .join(Jurisdiction, Jurisdiction.id == TaxRule.jurisdiction_id)
            .where(TaxRule.id.in_(rule_ids))
        )
        return {rule_id: code for rule_id, code in rows.all()}

    async def _citations(self, v: TaxRuleVersion) -> tuple[CitationSpec, ...]:
        if v.legislation_reference_id is None:
            return ()
        ref = await self.s.get(LegislationReference, v.legislation_reference_id)
        if ref is None:
            return ()
        return (CitationSpec(
            citation_text=ref.citation,
            legislation_reference_id=ref.id,
            source_url=ref.url or v.source_url,
        ),)

    # ---- unchanged evaluation internals -------------------------------------
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
        for node in by_id.values():
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


# ---- rules-layer determinations (kept here, never in a consumer) ------------
def _eligibility_status(
    basis_codes: tuple[str, ...], documents: tuple[DocumentSpec, ...]
) -> str:
    """The gate already passed; classify against contract-v2 authoring.

    No metadata ⇒ 'indeterminate' (consumers exclude rather than guess). Metadata
    present with a conditional document requirement ⇒ 'conditionally_eligible'.
    """
    if not basis_codes:
        return "indeterminate"
    if any(d.necessity == "conditional" for d in documents):
        return "conditionally_eligible"
    return "eligible"


def _expiry(v: TaxRuleVersion) -> ExpiryInfo:
    return ExpiryInfo(
        expiry_date=v.expiry_date,
        effective_date=v.effective_date,
        is_expiring=v.expiry_date is not None,
    )


def _projection_authorization(outcome: RuleOutcome) -> ProjectionAuthorization | None:
    """Copy the rule-authored projection metadata verbatim.

    Absence is reported as absence. A rule that says nothing about recurrence
    authorizes nothing, and the consumer must not read that as permission.
    """
    if not outcome.projection_eligibility:
        return None
    return ProjectionAuthorization(
        eligibility=outcome.projection_eligibility,
        method=outcome.projection_method,
        maximum_horizon=outcome.maximum_projection_horizon,
        required_assumption_codes=tuple(outcome.required_assumption_codes or ()),
    )


def _lever_ref(outcome: RuleOutcome) -> PortfolioLeverRef | None:
    """Rule data references a lever by CODE; it never names an engine field."""
    if not outcome.portfolio_lever_code:
        return None
    return PortfolioLeverRef(
        lever_code=outcome.portfolio_lever_code,
        parameter_bindings=dict(outcome.lever_parameters or {}),
    )
