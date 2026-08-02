"""IOE domain enums — stable, string-valued, canonically serializable.

Every member serializes as its lowercase snake_case *string value*, never an
ordinal, so a reordering of the class body can never change a stored hash
(architecture §9.1).

The contract-facing enums (calculation basis, economic effect, cost type,
reversibility, eligibility) MIRROR the rules-layer contract in
`app.services.tax_engine.contracts` rather than redefining them — the rules
layer remains the authority for their meaning.
"""
from __future__ import annotations

from enum import StrEnum


class CalculationBasis(StrEnum):
    """HOW a number was produced. Orthogonal to EvidenceStatus."""

    ENGINE_DETERMINED = "engine_determined"
    RULE_FORMULA_DETERMINED = "rule_formula_determined"
    SCENARIO_ESTIMATE = "scenario_estimate"
    PROJECTION_ESTIMATE = "projection_estimate"


class EvidenceStatus(StrEnum):
    """HOW WELL the inputs are supported. Never collapsed into CalculationBasis:
    a precisely calculated figure over unverified data must show both facts."""

    DOCUMENTED_VERIFIED = "documented_verified"
    DOCUMENTED_UNVERIFIED = "documented_unverified"
    USER_ATTESTED = "user_attested"
    INCOMPLETE = "incomplete"


class EconomicEffectType(StrEnum):
    IMMEDIATE_REFUND_IMPACT = "immediate_refund_impact"
    CURRENT_YEAR_TAX_REDUCTION = "current_year_tax_reduction"
    TAX_DEFERRAL = "tax_deferral"                       # timing, NOT a reduction
    REFUNDABLE_BENEFIT = "refundable_benefit"
    RECURRING_ANNUAL_BENEFIT = "recurring_annual_benefit"
    MULTI_YEAR_PROJECTED_BENEFIT = "multi_year_projected_benefit"
    FUTURE_OPTION_VALUE = "future_option_value"


class CostType(StrEnum):
    """A required cash contribution retains an asset and is therefore NOT a cost
    in the objective — only a liquidity constraint (architecture §B)."""

    REQUIRED_CASH_CONTRIBUTION = "required_cash_contribution"
    REQUIRED_EXPENDITURE = "required_expenditure"
    IMPLEMENTATION_COST = "implementation_cost"


class Reversibility(StrEnum):
    REVERSIBLE = "reversible"
    PARTIALLY_REVERSIBLE = "partially_reversible"
    IRREVERSIBLE = "irreversible"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    CONDITIONALLY_ELIGIBLE = "conditionally_eligible"
    INELIGIBLE = "ineligible"
    INDETERMINATE = "indeterminate"


class RelationshipType(StrEnum):
    REQUIRES = "requires"
    PRECEDES = "precedes"
    EXCLUDES = "excludes"
    SUBSTITUTES = "substitutes"
    SHARES_LIMIT = "shares_limit"
    ENHANCES = "enhances"
    REDUCES_VALUE = "reduces_value"
    OVERLAPS = "overlaps"


class PortfolioMembership(StrEnum):
    SELECTED = "selected"
    EXCLUDED_CONFLICT = "excluded_conflict"
    EXCLUDED_CONSTRAINT = "excluded_constraint"
    EXCLUDED_NOT_EVALUABLE = "excluded_not_evaluable"
    DEFERRED_TIMING = "deferred_timing"
    # A candidate that did not improve the objective ON ITS OWN at this point is
    # NOT discarded: near ceilings and thresholds it may become beneficial once
    # other candidates are applied (architecture §D.3).
    DEFERRED_PENDING_COMBINATION = "deferred_pending_combination"


class AssemblyMethod(StrEnum):
    GREEDY_RANKED = "greedy_ranked"
    GREEDY_RANKED_WITH_LOCAL_IMPROVEMENT = "greedy_ranked_with_local_improvement"


class OptimalityClaim(StrEnum):
    """There is deliberately no 'optimal' member: the assembler is rule-based."""

    NONE = "none"
    LOCALLY_IMPROVED = "locally_improved"


class AdditivityClass(StrEnum):
    ADDITIVE = "additive"
    SUB_ADDITIVE = "sub_additive"        # overlap — summing would OVERSTATE
    SUPER_ADDITIVE = "super_additive"    # synergy


class AttributionMethod(StrEnum):
    INCREMENTAL_PATH_DEPENDENT = "incremental_path_dependent"
    SHAPLEY_EXACT = "shapley_exact"


class ObjectiveMetric(StrEnum):
    CURRENT_YEAR_TAX_REDUCTION = "current_year_tax_reduction"
    CURRENT_YEAR_TAX_REDUCTION_NET_OF_EXPENDITURE = (
        "current_year_tax_reduction_net_of_expenditure"
    )
    NET_CASH_BENEFIT_CURRENT_YEAR = "net_cash_benefit_current_year"
    COMPARABLE_VALUE_MULTI_HORIZON = "comparable_value_multi_horizon"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FreshnessStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    SUPERSEDED = "superseded"


class VisibilityStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ReplayStatus(StrEnum):
    VERIFIED = "verified"
    DRIFTED = "drifted"
    UNAVAILABLE = "unavailable"


class AssumptionSource(StrEnum):
    USER = "user"
    PLATFORM = "platform"
    ANALYSIS = "analysis"


class AssumptionCertainty(StrEnum):
    USER_ASSERTED = "user_asserted"
    PLATFORM_DEFAULT = "platform_default"
    DERIVED_FROM_DATA = "derived_from_data"
    STATUTORY_KNOWN = "statutory_known"


class Materiality(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RecommendationLifecycle(StrEnum):
    NEW = "new"
    VIEWED = "viewed"
    SAVED = "saved"
    PLANNED = "planned"
    COMPLETED = "completed"
    DISMISSED = "dismissed"
    UNABLE_TO_COMPLETE = "unable_to_complete"
    EXPIRED = "expired"


class ScoreFactor(StrEnum):
    """Ranking factors. Each concept appears EXACTLY once (architecture §11):
    rule stability, documentation quality, and scenario/projection uncertainty
    live inside `confidence` and must not reappear here."""

    ECONOMIC_VALUE = "economic_value"
    CONFIDENCE = "confidence"
    TIME_SENSITIVITY = "time_sensitivity"
    IMPLEMENTATION_EFFORT = "implementation_effort"      # negative direction
    REQUIRED_CASH_FLOW = "required_cash_flow"            # negative direction
    USER_RELEVANCE = "user_relevance"
    REVERSIBILITY = "reversibility"


class ConfidenceFactor(StrEnum):
    """Confidence components. Precisely scoped so none overlaps another."""

    # evidence that the user MEETS the criteria
    ELIGIBILITY_EVIDENCE_STRENGTH = "eligibility_evidence_strength"
    # property of HOW the amount was produced
    CALCULATION_DETERMINISM = "calculation_determinism"
    # verification of documents supporting the AMOUNTS
    DOCUMENTATION_QUALITY = "documentation_quality"
    SCENARIO_UNCERTAINTY = "scenario_uncertainty"        # scenarios only
    PROJECTION_UNCERTAINTY = "projection_uncertainty"    # projections only
    RULE_STABILITY = "rule_stability"
