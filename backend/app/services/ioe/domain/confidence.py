"""Support/reliability scoring (architecture §14) — deterministic, explainable.

**This is a SUPPORT score, not a probability.** It expresses how well a result is
supported by the data, documentation, calculation basis, and rule stability
behind it. It is explicitly NOT a probability that the CRA will accept a claim,
nor a probability of receiving the displayed amount. `SUPPORT_SCORE_DISCLAIMER`
carries that statement to every surface that shows the number.

Computation is TWO-STAGE, and the stages are preserved separately because each
answers a different question:

  1. `raw_support_score` — weighted sum of the support components:

        eligibility_evidence_strength  evidence the user MEETS the criteria
        calculation_determinism        property of HOW the amount was produced
        documentation_quality          verification of documents behind AMOUNTS
        projection_uncertainty         horizon degradation (projections only)
        rule_stability                 expiry / amendment recency / validation

     Components that do not apply are OMITTED and their weight redistributed
     proportionally — scoring them zero would penalize a candidate for a factor
     that does not concern it.

  2. `assumption_adjusted_score` — `raw` reduced by an uncertainty adjustment
     computed from assumption **materiality, source, evidence, eligibility
     impact, and measured sensitivity**. It is NOT a function of how MANY
     assumptions there are: one high-materiality, eligibility-affecting,
     highly-sensitive assumption costs far more than five immaterial ones.

Finally `display_support_score = min(assumption_adjusted_score, CAP)`. The cap
applies to the DISPLAYED value only — the adjusted value is preserved so results
tied at the cap still order deterministically without the displayed number
taking on a different meaning.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.services.ioe.domain.enums import (
    AssumptionCertainty,
    AssumptionSource,
    CalculationBasis,
    ConfidenceFactor,
    EvidenceStatus,
    Materiality,
)
from app.services.ioe.domain.models import (
    ConfidenceBreakdown,
    ConfidenceComponent,
    StructuredAssumption,
    UncertaintyComponent,
)

CONFIDENCE_ALGORITHM_VERSION = "2.0.0"

SUPPORT_SCORE_DISCLAIMER = (
    "This is a support score: it reflects how well this result is backed by the "
    "information supplied, its documentation, and the published rule it relies "
    "on. It is not a probability that the CRA will accept a claim, and not a "
    "probability of receiving the amount shown."
)

# Ceiling for results that rest on assumptions. Applied as min() to the DISPLAYED
# value only (never as a blanket multiplier, which would penalize low-confidence
# results as much as high-confidence ones and change what the score means).
ASSUMPTION_DISPLAY_CAP = Decimal("80")
CAP_REASON_ASSUMPTION_BEARING = "ASSUMPTION_BEARING_RESULT"

_SCALE = Decimal("0.000001")
_PCT = Decimal("0.01")
_ONE = Decimal(1)

# Weights over the SUPPORT components only. Assumption uncertainty is a separate
# stage, so it deliberately has no weight here — including it as a component and
# adjusting for it afterwards would double-count the same doubt.
DEFAULT_WEIGHTS: dict[ConfidenceFactor, Decimal] = {
    ConfidenceFactor.ELIGIBILITY_EVIDENCE_STRENGTH: Decimal("0.35"),
    ConfidenceFactor.CALCULATION_DETERMINISM: Decimal("0.25"),
    ConfidenceFactor.DOCUMENTATION_QUALITY: Decimal("0.20"),
    ConfidenceFactor.RULE_STABILITY: Decimal("0.15"),
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

# ---- uncertainty inputs -----------------------------------------------------
_MATERIALITY_PENALTY: dict[Materiality, Decimal] = {
    Materiality.HIGH: Decimal("0.40"),
    Materiality.MEDIUM: Decimal("0.18"),
    Materiality.LOW: Decimal("0.06"),
}

# Where the assumption came from: a statutory-known value is barely an
# assumption; a bare user assertion carries the most doubt.
_SOURCE_MULTIPLIER: dict[AssumptionSource, Decimal] = {
    AssumptionSource.USER: Decimal("1.00"),
    AssumptionSource.ANALYSIS: Decimal("0.70"),
    AssumptionSource.PLATFORM: Decimal("0.60"),
}
_CERTAINTY_MULTIPLIER: dict[AssumptionCertainty, Decimal] = {
    AssumptionCertainty.USER_ASSERTED: Decimal("1.00"),
    AssumptionCertainty.PLATFORM_DEFAULT: Decimal("0.80"),
    AssumptionCertainty.DERIVED_FROM_DATA: Decimal("0.55"),
    AssumptionCertainty.STATUTORY_KNOWN: Decimal("0.20"),
}

# Weak evidence amplifies the doubt an assumption introduces.
_EVIDENCE_MULTIPLIER: dict[EvidenceStatus, Decimal] = {
    EvidenceStatus.DOCUMENTED_VERIFIED: Decimal("0.80"),
    EvidenceStatus.DOCUMENTED_UNVERIFIED: Decimal("1.00"),
    EvidenceStatus.USER_ATTESTED: Decimal("1.15"),
    EvidenceStatus.INCOMPLETE: Decimal("1.30"),
}

# An assumption that could change ELIGIBILITY matters more than one that only
# moves the amount.
_ELIGIBILITY_AMPLIFIER = Decimal("1.60")

_REASONS = {
    ConfidenceFactor.ELIGIBILITY_EVIDENCE_STRENGTH: "ELIGIBILITY_EVIDENCE",
    ConfidenceFactor.CALCULATION_DETERMINISM: "CALCULATION_BASIS",
    ConfidenceFactor.DOCUMENTATION_QUALITY: "DOCUMENTATION_VERIFICATION",
    ConfidenceFactor.PROJECTION_UNCERTAINTY: "PROJECTION_HORIZON",
    ConfidenceFactor.RULE_STABILITY: "RULE_STABILITY",
}


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


def assumption_penalty(
    assumption: StructuredAssumption, evidence_status: EvidenceStatus
) -> Decimal:
    """Uncertainty contributed by ONE assumption, in [0,1].

    Derived from materiality, source, certainty, supporting evidence, whether it
    affects eligibility, and measured sensitivity — never from the count.
    """
    penalty = _MATERIALITY_PENALTY.get(assumption.materiality, Decimal("0.18"))
    penalty *= _SOURCE_MULTIPLIER.get(assumption.source, Decimal("1.00"))
    penalty *= _CERTAINTY_MULTIPLIER.get(assumption.certainty, Decimal("1.00"))
    penalty *= _EVIDENCE_MULTIPLIER.get(evidence_status, Decimal("1.00"))
    if assumption.affects_eligibility:
        penalty *= _ELIGIBILITY_AMPLIFIER
    if assumption.sensitivity is not None:
        # A measured, insensitive result softens the penalty; a highly sensitive
        # one keeps it at full strength.
        penalty *= (Decimal("0.35") + Decimal("0.65") * _clamp01(assumption.sensitivity))
    return _clamp01(penalty)


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
    w = dict(weights or DEFAULT_WEIGHTS)

    # ---- stage 1: raw support ----
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
    is_projection = (
        calculation_basis is CalculationBasis.PROJECTION_ESTIMATE
        or (horizon_years is not None and horizon_years > 1)
    )
    if is_projection:
        values[ConfidenceFactor.PROJECTION_UNCERTAINTY] = projection_uncertainty(
            horizon_years or 1, indexation_known=indexation_known
        )

    applicable_weight = sum((w[f] for f in values), Decimal(0)) or _ONE

    components: list[ConfidenceComponent] = []
    raw_fraction = Decimal(0)
    for factor_code in sorted(values, key=lambda f: f.value):
        value = _clamp01(values[factor_code])
        weight = (w[factor_code] / applicable_weight).quantize(_SCALE, ROUND_HALF_UP)
        contribution = (value * weight).quantize(_SCALE, ROUND_HALF_UP)
        raw_fraction += contribution
        components.append(ConfidenceComponent(
            factor_code=factor_code, value=value, weight=weight,
            contribution=contribution, reason_code=_REASONS[factor_code],
        ))

    raw_support_score = (_clamp01(raw_fraction) * Decimal(100)).quantize(_PCT, ROUND_HALF_UP)

    # ---- stage 2: assumption uncertainty adjustment ----
    uncertainty: list[UncertaintyComponent] = []
    worst_penalty = Decimal(0)
    for a in sorted(assumptions, key=lambda x: x.code):
        penalty = assumption_penalty(a, evidence_status)
        worst_penalty = max(worst_penalty, penalty)
        uncertainty.append(UncertaintyComponent(
            assumption_code=a.code, materiality=a.materiality, source=a.source,
            affects_eligibility=a.affects_eligibility, sensitivity=a.sensitivity,
            penalty=penalty.quantize(_SCALE, ROUND_HALF_UP),
            reason_code=(
                "ELIGIBILITY_AFFECTING_ASSUMPTION" if a.affects_eligibility
                else "AMOUNT_AFFECTING_ASSUMPTION"
            ),
        ))

    assumption_adjusted_score = (
        raw_support_score * (_ONE - worst_penalty)
    ).quantize(_PCT, ROUND_HALF_UP)

    # ---- stage 3: display cap (min, never a blanket multiplier) ----
    rests_on_assumptions = bool(assumptions) or calculation_basis in (
        CalculationBasis.SCENARIO_ESTIMATE, CalculationBasis.PROJECTION_ESTIMATE,
    )
    display_support_score = assumption_adjusted_score
    cap_applied = False
    cap_reason_code: str | None = None
    if rests_on_assumptions:
        display_support_score = min(assumption_adjusted_score, ASSUMPTION_DISPLAY_CAP)
        cap_applied = display_support_score < assumption_adjusted_score
        if cap_applied:
            cap_reason_code = CAP_REASON_ASSUMPTION_BEARING

    return ConfidenceBreakdown(
        raw_support_score=raw_support_score,
        assumption_adjusted_score=assumption_adjusted_score,
        display_support_score=display_support_score,
        cap_applied=cap_applied,
        cap_reason_code=cap_reason_code,
        components=tuple(components),
        uncertainty=tuple(uncertainty),
    )


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal(0)
    if value > _ONE:
        return _ONE
    return value
