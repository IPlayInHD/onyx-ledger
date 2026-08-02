"""Confidence model (architecture §14) — deterministic, explainable, 0..100.

Six components, each precisely scoped so none overlaps another:

  eligibility_evidence_strength  evidence that the user MEETS the criteria
  calculation_determinism        property of HOW the amount was produced
  documentation_quality          verification of documents supporting AMOUNTS
  scenario_uncertainty           scenarios only
  projection_uncertainty         projections only
  rule_stability                 expiry proximity / amendment recency / validation

Two rules that shape the arithmetic:

* Components that do not apply are OMITTED and their weight is redistributed
  proportionally — scoring them zero would unfairly penalize, say, a non-scenario
  candidate for having no scenario uncertainty.
* Confidence is NOT reduced by the NUMBER of assumptions. One high-materiality
  assumption with high result sensitivity costs far more than five low-materiality
  ones. Assumptions that affect ELIGIBILITY weigh more than those affecting only
  the amount.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.services.ioe.domain.enums import (
    CalculationBasis,
    ConfidenceFactor,
    EvidenceStatus,
    Materiality,
)
from app.services.ioe.domain.models import (
    ConfidenceBreakdown,
    ConfidenceComponent,
    StructuredAssumption,
)

CONFIDENCE_ALGORITHM_VERSION = "1.0.0"

_SCALE = Decimal("0.000001")
_ONE = Decimal(1)

DEFAULT_WEIGHTS: dict[ConfidenceFactor, Decimal] = {
    ConfidenceFactor.ELIGIBILITY_EVIDENCE_STRENGTH: Decimal("0.30"),
    ConfidenceFactor.CALCULATION_DETERMINISM: Decimal("0.25"),
    ConfidenceFactor.DOCUMENTATION_QUALITY: Decimal("0.15"),
    ConfidenceFactor.RULE_STABILITY: Decimal("0.15"),
    ConfidenceFactor.SCENARIO_UNCERTAINTY: Decimal("0.10"),
    ConfidenceFactor.PROJECTION_UNCERTAINTY: Decimal("0.05"),
}

# Evidence supporting ELIGIBILITY.
_ELIGIBILITY_EVIDENCE: dict[EvidenceStatus, Decimal] = {
    EvidenceStatus.DOCUMENTED_VERIFIED: Decimal("1.00"),
    EvidenceStatus.DOCUMENTED_UNVERIFIED: Decimal("0.75"),
    EvidenceStatus.USER_ATTESTED: Decimal("0.55"),
    EvidenceStatus.INCOMPLETE: Decimal("0.20"),
}

# Verification of the documents supporting the AMOUNTS (a distinct question).
_DOCUMENTATION_QUALITY: dict[EvidenceStatus, Decimal] = {
    EvidenceStatus.DOCUMENTED_VERIFIED: Decimal("1.00"),
    EvidenceStatus.DOCUMENTED_UNVERIFIED: Decimal("0.60"),
    EvidenceStatus.USER_ATTESTED: Decimal("0.40"),
    EvidenceStatus.INCOMPLETE: Decimal("0.15"),
}

_DETERMINISM: dict[CalculationBasis, Decimal] = {
    CalculationBasis.ENGINE_DETERMINED: Decimal("1.00"),
    CalculationBasis.RULE_FORMULA_DETERMINED: Decimal("1.00"),
    CalculationBasis.SCENARIO_ESTIMATE: Decimal("0.70"),
    CalculationBasis.PROJECTION_ESTIMATE: Decimal("0.45"),
}

_MATERIALITY_IMPACT: dict[Materiality, Decimal] = {
    Materiality.HIGH: Decimal("0.45"),
    Materiality.MEDIUM: Decimal("0.20"),
    Materiality.LOW: Decimal("0.07"),
}

_REASONS = {
    ConfidenceFactor.ELIGIBILITY_EVIDENCE_STRENGTH: "ELIGIBILITY_EVIDENCE",
    ConfidenceFactor.CALCULATION_DETERMINISM: "CALCULATION_BASIS",
    ConfidenceFactor.DOCUMENTATION_QUALITY: "DOCUMENTATION_VERIFICATION",
    ConfidenceFactor.SCENARIO_UNCERTAINTY: "SCENARIO_ASSUMPTIONS",
    ConfidenceFactor.PROJECTION_UNCERTAINTY: "PROJECTION_HORIZON",
    ConfidenceFactor.RULE_STABILITY: "RULE_STABILITY",
}

# Assumption-bearing results are held below a ceiling so certainty is never
# implied where assumptions exist (decision D-5 / architecture §15).
#
# Applied as a PROPORTIONAL ceiling rather than a clip: a hard clip would collapse
# every well-evidenced assumption-bearing result to exactly the cap, destroying
# the ordering between them and making confidence useless for ranking precisely
# where uncertainty matters most. Scaling guarantees the same ceiling while
# preserving relative ordering.
ASSUMPTION_CONFIDENCE_CAP = Decimal("80")


def scenario_uncertainty(assumptions: tuple[StructuredAssumption, ...]) -> Decimal:
    """1.0 = no meaningful uncertainty. Driven by materiality, sensitivity, and
    whether an assumption touches eligibility — NOT by how many there are."""
    if not assumptions:
        return _ONE
    worst = Decimal(0)
    for a in assumptions:
        impact = _MATERIALITY_IMPACT.get(a.materiality, Decimal("0.20"))
        if a.sensitivity is not None:
            # a measured, insensitive result softens the penalty
            impact *= (Decimal("0.4") + Decimal("0.6") * _clamp01(a.sensitivity))
        if a.affects_eligibility:
            impact *= Decimal("1.5")     # eligibility doubt outweighs amount doubt
        worst = max(worst, impact)
    return _clamp01(_ONE - worst)


def projection_uncertainty(horizon_years: int, *, indexation_known: bool = False) -> Decimal:
    """Degrades with horizon; a known indexation factor degrades more slowly."""
    if horizon_years <= 1:
        return _ONE
    per_year = Decimal("0.08") if indexation_known else Decimal("0.14")
    return _clamp01(_ONE - per_year * Decimal(horizon_years - 1))


def rule_stability(
    *, months_to_expiry: int | None = None, validation_status: str = "passed",
    months_since_amendment: int | None = None,
) -> Decimal:
    value = _ONE
    if months_to_expiry is not None and months_to_expiry < 24:
        value -= Decimal("0.25") * (Decimal(24 - max(months_to_expiry, 0)) / Decimal(24))
    if months_since_amendment is not None and months_since_amendment < 12:
        value -= Decimal("0.10")
    if validation_status == "warnings":
        value -= Decimal("0.10")
    elif validation_status not in ("passed", "warnings"):
        value -= Decimal("0.30")
    return _clamp01(value)


def compute(
    *,
    evidence_status: EvidenceStatus,
    calculation_basis: CalculationBasis,
    assumptions: tuple[StructuredAssumption, ...] = (),
    horizon_years: int | None = None,
    indexation_known: bool = False,
    rule_stability_value: Decimal | None = None,
    weights: dict[ConfidenceFactor, Decimal] | None = None,
) -> ConfidenceBreakdown:
    """Compute confidence with proportional redistribution over applicable factors."""
    w = dict(weights or DEFAULT_WEIGHTS)

    values: dict[ConfidenceFactor, Decimal] = {
        ConfidenceFactor.ELIGIBILITY_EVIDENCE_STRENGTH:
            _ELIGIBILITY_EVIDENCE.get(evidence_status, Decimal("0.20")),
        ConfidenceFactor.CALCULATION_DETERMINISM:
            _DETERMINISM.get(calculation_basis, Decimal("0.50")),
        ConfidenceFactor.DOCUMENTATION_QUALITY:
            _DOCUMENTATION_QUALITY.get(evidence_status, Decimal("0.15")),
        ConfidenceFactor.RULE_STABILITY:
            rule_stability_value if rule_stability_value is not None else _ONE,
    }

    # Scenario uncertainty applies only when the result rests on assumptions.
    is_scenario = calculation_basis is CalculationBasis.SCENARIO_ESTIMATE or bool(assumptions)
    if is_scenario:
        values[ConfidenceFactor.SCENARIO_UNCERTAINTY] = scenario_uncertainty(assumptions)

    # Projection uncertainty applies only to multi-year projections.
    is_projection = (
        calculation_basis is CalculationBasis.PROJECTION_ESTIMATE
        or (horizon_years is not None and horizon_years > 1)
    )
    if is_projection:
        values[ConfidenceFactor.PROJECTION_UNCERTAINTY] = projection_uncertainty(
            horizon_years or 1, indexation_known=indexation_known
        )

    # Redistribute the weight of omitted factors proportionally.
    applicable_weight = sum((w[f] for f in values), Decimal(0))
    if applicable_weight <= 0:  # pragma: no cover - defensive
        applicable_weight = _ONE

    components: list[ConfidenceComponent] = []
    overall = Decimal(0)
    for factor_code in sorted(values, key=lambda f: f.value):
        value = _clamp01(values[factor_code])
        weight = (w[factor_code] / applicable_weight).quantize(_SCALE, ROUND_HALF_UP)
        contribution = (value * weight).quantize(_SCALE, ROUND_HALF_UP)
        overall += contribution
        components.append(ConfidenceComponent(
            factor_code=factor_code, value=value, weight=weight,
            contribution=contribution, reason_code=_REASONS[factor_code],
        ))

    score = (_clamp01(overall) * Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP)

    # A result resting on assumptions — declared ones, or a scenario/projection
    # basis — can never present as certain.
    rests_on_assumptions = bool(assumptions) or calculation_basis in (
        CalculationBasis.SCENARIO_ESTIMATE, CalculationBasis.PROJECTION_ESTIMATE,
    )
    if rests_on_assumptions:
        score = (score * ASSUMPTION_CONFIDENCE_CAP / Decimal(100)).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )
    return ConfidenceBreakdown(overall=score, components=tuple(components))


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal(0)
    if value > _ONE:
        return _ONE
    return value
