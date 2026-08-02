"""IOE domain value objects — pure, framework-free, no I/O and no clock.

These describe what the engine computed and why. Their `as_canonical()` methods
feed the hash payloads in `canonical.py`, so every field that participates in a
hash goes through an explicit scale helper.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.enums import (
    AdditivityClass,
    AssemblyMethod,
    AssumptionCertainty,
    AssumptionSource,
    CalculationBasis,
    ConfidenceFactor,
    CostType,
    EconomicEffectType,
    EligibilityStatus,
    EvidenceStatus,
    Materiality,
    ObjectiveMetric,
    OptimalityClaim,
    PortfolioMembership,
    RelationshipType,
    Reversibility,
    ScoreFactor,
)


# ---------------------------------------------------------------------------
# Economics
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EconomicEffect:
    """One economic outcome. A candidate may carry several (e.g. an RRSP
    contribution is both a current-year reduction AND a deferral); they are
    never collapsed into a single number."""

    effect_type: EconomicEffectType
    amount: Decimal
    calculation_basis: CalculationBasis
    tax_year: int | None = None
    horizon_years: int = 1
    reversibility: Reversibility | None = None
    is_permanent: bool = True

    def as_canonical(self) -> dict:
        return {
            "effect_type": self.effect_type,
            "amount": c.money(self.amount),
            "calculation_basis": self.calculation_basis,
            "tax_year": self.tax_year,
            "horizon_years": self.horizon_years,
            "reversibility": self.reversibility,
            "is_permanent": self.is_permanent,
        }


@dataclass(frozen=True)
class CostComponent:
    cost_type: CostType
    amount: Decimal
    timing: str | None = None

    @property
    def is_true_cost(self) -> bool:
        """A required cash CONTRIBUTION retains the asset, so it is a liquidity
        constraint rather than a cost. Only expenditure and implementation cost
        reduce the objective (architecture §B)."""
        return self.cost_type is not CostType.REQUIRED_CASH_CONTRIBUTION

    def as_canonical(self) -> dict:
        return {
            "cost_type": self.cost_type,
            "amount": c.money(self.amount),
            "timing": self.timing,
        }


# ---------------------------------------------------------------------------
# Score / confidence provenance
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScoreComponent:
    factor_code: ScoreFactor
    raw_value: Decimal
    normalized_value: Decimal
    weight: Decimal
    contribution: Decimal

    def as_canonical(self) -> dict:
        return {
            "factor_code": self.factor_code,
            "raw_value": c.factor(self.raw_value),
            "normalized_value": c.factor(self.normalized_value),
            "weight": c.factor(self.weight),
            "contribution": c.factor(self.contribution),
        }


@dataclass(frozen=True)
class ScoreBreakdown:
    overall: Decimal                       # 0..100
    components: tuple[ScoreComponent, ...]

    def as_canonical(self) -> dict:
        return {
            "overall": c.factor(self.overall),
            "components": [comp.as_canonical() for comp in self.components],
        }


@dataclass(frozen=True)
class ConfidenceComponent:
    factor_code: ConfidenceFactor
    value: Decimal
    weight: Decimal
    contribution: Decimal
    reason_code: str

    def as_canonical(self) -> dict:
        return {
            "factor_code": self.factor_code,
            "value": c.factor(self.value),
            "weight": c.factor(self.weight),
            "contribution": c.factor(self.contribution),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class UncertaintyComponent:
    """One assumption's contribution to the uncertainty adjustment.

    Kept separate from the support components so the raw weighted sum stays
    verifiable on its own and the adjustment is independently auditable.
    """

    assumption_code: str
    materiality: Materiality
    source: AssumptionSource
    affects_eligibility: bool
    sensitivity: Decimal | None
    penalty: Decimal
    reason_code: str

    def as_canonical(self) -> dict:
        return {
            "assumption_code": self.assumption_code,
            "materiality": self.materiality,
            "source": self.source,
            "affects_eligibility": self.affects_eligibility,
            "sensitivity": c.rate(self.sensitivity),
            "penalty": c.factor(self.penalty),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """A SUPPORT/RELIABILITY score — explicitly not a probability.

    Three values are preserved rather than collapsed, because each answers a
    different question and conflating them changes the meaning of the model:

      raw_support_score          how well the result is supported, before
                                 accounting for declared assumptions
      assumption_adjusted_score  after the uncertainty adjustment derived from
                                 assumption materiality, source, evidence,
                                 eligibility impact, and measured sensitivity
      display_support_score      min(assumption_adjusted_score, cap) — the
                                 user-facing value

    The cap is applied ONLY to the displayed value. Ordering uses
    `assumption_adjusted_score`, so results tied at the displayed cap still order
    deterministically without the displayed number changing meaning.
    """

    raw_support_score: Decimal              # 0..100
    assumption_adjusted_score: Decimal      # 0..100
    display_support_score: Decimal          # 0..100, capped
    cap_applied: bool
    cap_reason_code: str | None
    components: tuple[ConfidenceComponent, ...]
    uncertainty: tuple[UncertaintyComponent, ...] = ()

    @property
    def overall(self) -> Decimal:
        """The user-facing value. Alias for `display_support_score`."""
        return self.display_support_score

    def as_canonical(self) -> dict:
        return {
            "raw_support_score": c.factor(self.raw_support_score),
            "assumption_adjusted_score": c.factor(self.assumption_adjusted_score),
            "display_support_score": c.factor(self.display_support_score),
            "cap_applied": self.cap_applied,
            "cap_reason_code": self.cap_reason_code,
            "components": [comp.as_canonical() for comp in self.components],
            "uncertainty": [u.as_canonical() for u in self.uncertainty],
        }


# ---------------------------------------------------------------------------
# Assumptions (structured; wording never affects a calculation)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StructuredAssumption:
    code: str
    value: object
    source: AssumptionSource
    certainty: AssumptionCertainty
    materiality: Materiality = Materiality.MEDIUM
    effective_period: str | None = None
    affects_eligibility: bool = False
    sensitivity: Decimal | None = None      # measured, not guessed
    display_note: str | None = None         # presentation only

    def as_canonical(self) -> dict:
        # display_note is deliberately EXCLUDED: rewording a note must never
        # change the identity of a calculation.
        return {
            "code": self.code,
            "value": self.value,
            "source": self.source,
            "certainty": self.certainty,
            "materiality": self.materiality,
            "effective_period": self.effective_period,
            "affects_eligibility": self.affects_eligibility,
            "sensitivity": c.rate(self.sensitivity),
        }


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LeverApplication:
    """A resolved lever plus its parameters, ready to apply to a cloned input."""

    lever_code: str
    parameters: dict[str, Decimal | str] = field(default_factory=dict)

    def as_canonical(self) -> dict:
        return {
            "lever_code": self.lever_code,
            "parameters": {
                k: (c.money(v) if isinstance(v, Decimal) else v)
                for k, v in self.parameters.items()
            },
        }


@dataclass
class OptimizationCandidate:
    """A normalized opportunity plus IOE-owned, non-legal metadata.

    Legal fields (`eligibility_status`, actions, deadlines, citations) are copied
    verbatim from the rules-layer contract; nothing here infers them.
    """

    candidate_key: str                       # stable, deterministic identity
    opportunity_code: str
    rule_version_id: str | None
    eligibility_status: EligibilityStatus
    evidence_status: EvidenceStatus = EvidenceStatus.INCOMPLETE
    calculation_basis: CalculationBasis | None = None

    economic_effects: tuple[EconomicEffect, ...] = ()
    costs: tuple[CostComponent, ...] = ()
    shared_resource_codes: tuple[str, ...] = ()
    lever_application: LeverApplication | None = None

    effort_rating: int = 3                   # rules-supplied (1..5)
    days_to_deadline: int | None = None      # from rules-supplied deadlines
    user_relevant: bool = True
    reversibility: Reversibility | None = None
    requires_codes: tuple[str, ...] = ()
    excludes_codes: tuple[str, ...] = ()

    # computed
    score: ScoreBreakdown | None = None
    confidence: ConfidenceBreakdown | None = None
    standalone_potential: Decimal | None = None
    incremental_portfolio_benefit: Decimal | None = None
    portfolio_membership: PortfolioMembership = PortfolioMembership.EXCLUDED_NOT_EVALUABLE
    exclusion_reason_code: str | None = None
    rank: int | None = None

    @property
    def is_portfolio_evaluable(self) -> bool:
        return (
            self.lever_application is not None
            and self.eligibility_status in (
                EligibilityStatus.ELIGIBLE, EligibilityStatus.CONDITIONALLY_ELIGIBLE,
            )
        )

    def required_cash_contribution(self) -> Decimal:
        return sum(
            (x.amount for x in self.costs
             if x.cost_type is CostType.REQUIRED_CASH_CONTRIBUTION),
            Decimal(0),
        )

    def true_costs(self) -> Decimal:
        """Expenditure + implementation cost. Excludes contributions (§B)."""
        return sum((x.amount for x in self.costs if x.is_true_cost), Decimal(0))

    def as_canonical(self) -> dict:
        return {
            "candidate_key": self.candidate_key,
            "opportunity_code": self.opportunity_code,
            "rule_version_id": self.rule_version_id,
            "eligibility_status": self.eligibility_status,
            "evidence_status": self.evidence_status,
            "calculation_basis": self.calculation_basis,
            "economic_effects": [e.as_canonical() for e in self.economic_effects],
            "costs": [x.as_canonical() for x in self.costs],
            "shared_resource_codes": list(self.shared_resource_codes),
            "lever_application": (
                self.lever_application.as_canonical() if self.lever_application else None
            ),
            "score": self.score.as_canonical() if self.score else None,
            "confidence": self.confidence.as_canonical() if self.confidence else None,
            "standalone_potential": c.money(self.standalone_potential),
            "incremental_portfolio_benefit": c.money(self.incremental_portfolio_benefit),
            "portfolio_membership": self.portfolio_membership,
            "exclusion_reason_code": self.exclusion_reason_code,
            "rank": self.rank,
        }


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecommendationRelationship:
    source_key: str
    target_key: str
    relationship_type: RelationshipType
    explanation_code: str
    shared_resource_code: str | None = None
    maximum_shared_amount: Decimal | None = None
    measured_delta: Decimal | None = None
    resolution_options: tuple[str, ...] = ()

    def as_canonical(self) -> dict:
        return {
            "source_key": self.source_key,
            "target_key": self.target_key,
            "relationship_type": self.relationship_type,
            "explanation_code": self.explanation_code,
            "shared_resource_code": self.shared_resource_code,
            "maximum_shared_amount": c.money(self.maximum_shared_amount),
            "measured_delta": c.money(self.measured_delta),
            "resolution_options": list(self.resolution_options),
        }


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PortfolioMember:
    candidate_key: str
    apply_order: int
    incremental_benefit: Decimal
    resource_allocations: tuple[tuple[str, Decimal], ...] = ()

    def as_canonical(self) -> dict:
        return {
            "candidate_key": self.candidate_key,
            "apply_order": self.apply_order,
            "incremental_benefit": c.money(self.incremental_benefit),
            "resource_allocations": [
                {"resource_code": code, "amount": c.money(amount)}
                for code, amount in self.resource_allocations
            ],
        }


@dataclass(frozen=True)
class LedgerEntry:
    resource_code: str
    capacity: Decimal | None
    allocated: Decimal
    remaining: Decimal | None

    def as_canonical(self) -> dict:
        return {
            "resource_code": self.resource_code,
            "capacity": c.money(self.capacity),
            "allocated": c.money(self.allocated),
            "remaining": c.money(self.remaining),
        }


@dataclass(frozen=True)
class StrategyPortfolio:
    """The assembled strategy set.

    `portfolio_total_benefit` comes from a COMBINED engine run and is the only
    figure that may be shown as a total. `sum_of_standalone` is retained purely
    as a diagnostic and must never be displayed.
    """

    members: tuple[PortfolioMember, ...]
    ledger: tuple[LedgerEntry, ...]
    baseline_tax: Decimal
    portfolio_tax: Decimal
    portfolio_total_benefit: Decimal
    sum_of_standalone: Decimal
    interaction_delta: Decimal
    additivity_class: AdditivityClass
    additivity_verified: bool
    objective_metric: ObjectiveMetric
    objective_value_baseline: Decimal
    objective_value_final: Decimal
    assembly_method: AssemblyMethod
    optimality_claim: OptimalityClaim
    deferred_count: int = 0
    excluded_count: int = 0
    improvement_moves_applied: int = 0
    improvement_runs_used: int = 0
    engine_runs_used: int = 0
    unexplored_alternatives_count: int = 0

    def as_canonical(self) -> dict:
        return {
            "members": [m.as_canonical() for m in self.members],
            "ledger": [entry.as_canonical() for entry in self.ledger],
            "baseline_tax": c.money(self.baseline_tax),
            "portfolio_tax": c.money(self.portfolio_tax),
            "portfolio_total_benefit": c.money(self.portfolio_total_benefit),
            "sum_of_standalone": c.money(self.sum_of_standalone),
            "interaction_delta": c.money(self.interaction_delta),
            "additivity_class": self.additivity_class,
            "additivity_verified": self.additivity_verified,
            "objective_metric": self.objective_metric,
            "objective_value_baseline": c.money(self.objective_value_baseline),
            "objective_value_final": c.money(self.objective_value_final),
            "assembly_method": self.assembly_method,
            "optimality_claim": self.optimality_claim,
            "deferred_count": self.deferred_count,
            "excluded_count": self.excluded_count,
            "improvement_moves_applied": self.improvement_moves_applied,
            "engine_runs_used": self.engine_runs_used,
            "unexplored_alternatives_count": self.unexplored_alternatives_count,
        }
