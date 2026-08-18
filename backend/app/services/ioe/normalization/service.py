"""OpportunityNormalizationService — contract v2 → OptimizationCandidate.

Normalizes, classifies (non-legally), and enriches with IOE-owned metadata. It
detects nothing legal, which is why it is not called a detection service.

The rule it exists to enforce: **legally meaningful fields are copied verbatim
and never invented.** Where the rules layer supplied nothing, the candidate
records nothing — no default eligibility, no guessed deadline, no assumed
economic effect type. A candidate whose contract fields are absent is
`indeterminate` and not portfolio-evaluable, so it is surfaced as informational
rather than acted on.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.ioe.domain.enums import (
    CalculationBasis,
    CostType,
    EconomicEffectType,
    EligibilityStatus,
    EvidenceStatus,
    Reversibility,
)
from app.services.ioe.domain.models import (
    CostComponent,
    EconomicEffect,
    LeverApplication,
    OptimizationCandidate,
)
from app.services.tax_engine.contracts import OpportunityContractV2

NORMALIZATION_VERSION = "1.0.0"


class OpportunityNormalizationService:
    def __init__(self, *, today: date | None = None):
        # injected rather than read from the clock, so normalization is pure
        self._today = today

    def normalize(
        self,
        opportunity: OpportunityContractV2,
        *,
        evidence_status: EvidenceStatus = EvidenceStatus.INCOMPLETE,
        user_relevant: bool = True,
    ) -> OptimizationCandidate:
        return OptimizationCandidate(
            candidate_key=self.candidate_key(opportunity),
            opportunity_code=opportunity.opportunity_code,
            rule_version_id=(
                str(opportunity.rule_version_id) if opportunity.rule_version_id else None
            ),
            # ---- copied verbatim from the rules layer ----
            eligibility_status=_eligibility(opportunity.eligibility_status),
            calculation_basis=_basis(opportunity.calculation_basis),
            evidence_status=evidence_status,
            shared_resource_codes=tuple(opportunity.shared_resource_codes),
            reversibility=_reversibility(opportunity.reversibility),
            requires_codes=tuple(
                d.depends_on_rule_code.lower() for d in opportunity.dependencies
                if d.dependency_type == "requires"
            ),
            excludes_codes=tuple(
                d.depends_on_rule_code.lower() for d in opportunity.dependencies
                if d.dependency_type in ("excludes", "substitutes")
            ),
            effort_rating=self._effort(opportunity),
            days_to_deadline=self._days_to_deadline(opportunity),
            # ---- IOE-owned, non-legal derivations ----
            economic_effects=self._effects(opportunity),
            costs=self._costs(opportunity),
            lever_application=self._lever(opportunity),
            user_relevant=user_relevant,
        )

    @staticmethod
    def candidate_key(opportunity: OpportunityContractV2) -> str:
        """Deterministic identity: stable across runs for the same opportunity.

        `opportunity_code` names the RULE, so `code:version` identifies a rule
        version rather than an opportunity. That was enough for as long as a
        version emitted one opportunity and wrong the moment one emitted two:
        both got the same key, `key_to_id` kept only the last id, and the two
        portfolio members that resulted both pointed at it — a unique violation
        on `(portfolio_id, candidate_id)` that rolled back the whole run.

        The discriminator is appended ONLY when the rules layer supplied one,
        which it does only for a version emitting more than one opportunity. So
        every key sealed before this is reproduced byte for byte, and the two
        opportunities of a two-outcome version are finally distinguishable.
        """
        base = f"{opportunity.opportunity_code}:{opportunity.rule_version_id}"
        if opportunity.outcome_discriminator is None:
            return base
        return f"{base}:{opportunity.outcome_discriminator}"

    # ---- derivations --------------------------------------------------------
    def _effects(self, o: OpportunityContractV2) -> tuple[EconomicEffect, ...]:
        """One effect, only when BOTH an amount and a rules-supplied effect type
        exist. An unclassified amount is not silently assumed to be a current-year
        reduction — that classification is the rules layer's to make."""
        if o.calculated_impact is None or not o.economic_effect_type:
            return ()
        effect_type = _effect_type(o.economic_effect_type)
        if effect_type is None:
            return ()
        return (EconomicEffect(
            effect_type=effect_type,
            amount=o.calculated_impact,
            calculation_basis=_basis(o.calculation_basis) or CalculationBasis.RULE_FORMULA_DETERMINED,
            tax_year=o.tax_year,
            horizon_years=1,
            reversibility=_reversibility(o.reversibility),
            is_permanent=effect_type is not EconomicEffectType.TAX_DEFERRAL,
        ),)

    def _costs(self, o: OpportunityContractV2) -> tuple[CostComponent, ...]:
        """Costs come from rules-supplied ActionSpecs; none are imputed.

        The AMOUNT is always the rule's. The cost TYPE may be a deterministic
        resolution of an ambiguous legacy value, so the authored value and the
        basis of the resolution travel with it (P4 item 3).
        """
        from app.services.ioe.domain.cost_taxonomy import resolve_cost_type

        lever_code = o.portfolio_lever_ref.lever_code if o.portfolio_lever_ref else None
        out: list[CostComponent] = []
        for action in o.required_actions:
            if action.cost_type and action.cost_amount is not None:
                cost_type = _cost_type(action.cost_type)
                if cost_type is not None:
                    resolved = resolve_cost_type(cost_type, lever_code=lever_code)
                    out.append(CostComponent(
                        cost_type=resolved.cost_type,
                        amount=action.cost_amount,
                        authored_cost_type=resolved.authored_cost_type,
                        cost_type_source=resolved.source,
                        taxonomy_version=resolved.taxonomy_version,
                    ))
        return tuple(out)

    def _lever(self, o: OpportunityContractV2) -> LeverApplication | None:
        """Resolve the rules-supplied lever REFERENCE. The registry owns what the
        lever does; an unknown code makes the candidate non-evaluable rather than
        guessed (architecture §C)."""
        from app.services.ioe.domain import levers

        ref = o.portfolio_lever_ref
        if ref is None or not levers.exists(ref.lever_code):
            return None
        parameters = self._resolve_parameters(o, ref.parameter_bindings)
        if parameters is None:
            return None
        return LeverApplication(lever_code=ref.lever_code, parameters=parameters)

    def _resolve_parameters(
        self, o: OpportunityContractV2, bindings: dict[str, str]
    ) -> dict[str, Decimal | str] | None:
        """Bind declared parameter sources to values already supplied by the rules
        layer. An unresolvable binding yields None — never a fabricated amount."""
        sources: dict[str, object] = {
            "rule.calculated_impact": o.calculated_impact,
            "rule.max_amount": o.calculated_impact,
            "rule.jurisdiction": o.jurisdiction,
        }
        for action in o.required_actions:
            if action.cost_amount is not None:
                sources.setdefault("action.cost_amount", action.cost_amount)

        resolved: dict[str, Decimal | str] = {}
        for name, source_key in bindings.items():
            value = sources.get(source_key)
            if value is None:
                return None
            # `sources` is a Mapping[str, Decimal | str]; `.get` widens to
            # object once a default is in play, so the narrowing is restated.
            assert isinstance(value, (Decimal, str))
            resolved[name] = value
        return resolved or None

    @staticmethod
    def _effort(o: OpportunityContractV2) -> int:
        """Effort is rules-supplied; with no actions declared, use the neutral
        midpoint rather than inventing a difficulty."""
        ratings = [a.effort_rating for a in o.required_actions if a.effort_rating]
        return max(ratings) if ratings else 3

    def _days_to_deadline(self, o: OpportunityContractV2) -> int | None:
        """Nearest HARD deadline, in days. None when the rules layer declared
        none — absence is reported, not replaced with a default urgency."""
        if self._today is None:
            return None
        dates = [
            d.deadline_date for d in o.applicable_deadlines
            if d.is_hard and d.deadline_date is not None
        ]
        if not dates:
            return None
        return (min(dates) - self._today).days


# ---- verbatim mappers: unknown/absent values map to None, never to a default --
def _eligibility(value: str | None) -> EligibilityStatus:
    try:
        return EligibilityStatus(value) if value else EligibilityStatus.INDETERMINATE
    except ValueError:
        return EligibilityStatus.INDETERMINATE


def _basis(value: str | None) -> CalculationBasis | None:
    try:
        return CalculationBasis(value) if value else None
    except ValueError:
        return None


def _effect_type(value: str | None) -> EconomicEffectType | None:
    try:
        return EconomicEffectType(value) if value else None
    except ValueError:
        return None


def _cost_type(value: str | None) -> CostType | None:
    try:
        return CostType(value) if value else None
    except ValueError:
        return None


def _reversibility(value: str | None) -> Reversibility | None:
    try:
        return Reversibility(value) if value else None
    except ValueError:
        return None
