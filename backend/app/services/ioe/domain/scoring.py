"""Candidate ranking (architecture §11) — stage 1 of two.

Ranking ORDERS candidates; it does not select them. Selection is the constrained
portfolio assembly in `portfolio.py`.

    RecommendationScore = clamp01( Σ w_i · f_i ) × 100

Each concept appears EXACTLY once. Rule stability, documentation quality, and
scenario/projection uncertainty are deliberately absent here: they live inside
`confidence`, and repeating them would double-weight them.

Historical user behaviour is absent by design (decision D-7). Prior acceptance or
dismissal must not move a financial figure; it is carried as presentation
metadata for the client to re-order or filter with, and never enters this score.

Negative-direction factors contribute their COMPLEMENT (1 − value), so all
weights are positive, they sum to 1, and a perfect candidate scores 100.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.services.ioe.domain.enums import Reversibility, ScoreFactor
from app.services.ioe.domain.models import (
    OptimizationCandidate,
    ScoreBreakdown,
    ScoreComponent,
)
from app.services.ioe.domain.savings import comparable_value

SCORING_ALGORITHM_VERSION = "1.0.0"

_SCALE = Decimal("0.000001")
_ONE = Decimal(1)

# Weights are DATA (pinned per run via weight_config); these are the defaults
# that seed weight_config v1. The ALGORITHM version above is separate.
DEFAULT_WEIGHTS: dict[ScoreFactor, Decimal] = {
    ScoreFactor.ECONOMIC_VALUE: Decimal("0.35"),
    ScoreFactor.CONFIDENCE: Decimal("0.20"),
    ScoreFactor.TIME_SENSITIVITY: Decimal("0.15"),
    ScoreFactor.IMPLEMENTATION_EFFORT: Decimal("0.10"),   # negative direction
    ScoreFactor.REQUIRED_CASH_FLOW: Decimal("0.05"),      # negative direction
    ScoreFactor.USER_RELEVANCE: Decimal("0.10"),
    ScoreFactor.REVERSIBILITY: Decimal("0.05"),
}

NEGATIVE_DIRECTION = frozenset({
    ScoreFactor.IMPLEMENTATION_EFFORT, ScoreFactor.REQUIRED_CASH_FLOW,
})

# Normalization cap for economic value: $5,000 of comparable value scores 1.0.
ECONOMIC_VALUE_CAP = Decimal("5000")

_REVERSIBILITY_VALUE: dict[Reversibility, Decimal] = {
    Reversibility.REVERSIBLE: Decimal("1.00"),
    Reversibility.PARTIALLY_REVERSIBLE: Decimal("0.60"),
    Reversibility.IRREVERSIBLE: Decimal("0.30"),
}


class WeightConfigError(ValueError):
    """The supplied weight configuration is not usable."""


def validate_weights(weights: dict[ScoreFactor, Decimal]) -> dict[ScoreFactor, Decimal]:
    """Every factor present exactly once, each in [0,1], summing to 1.

    Weights that do not total 1 are rejected rather than silently rescaled, so a
    misconfiguration cannot quietly distort every ranking.
    """
    missing = set(ScoreFactor) - set(weights)
    if missing:
        raise WeightConfigError(
            f"weight config is missing factor(s): {sorted(f.value for f in missing)}"
        )
    unknown = set(weights) - set(ScoreFactor)
    if unknown:
        raise WeightConfigError(f"weight config has unknown factor(s): {sorted(unknown)}")
    for factor_code, weight in weights.items():
        if weight < 0 or weight > 1:
            raise WeightConfigError(
                f"weight for '{factor_code.value}' must be within [0,1], got {weight}"
            )
    total = sum(weights.values(), Decimal(0))
    if total != _ONE:
        raise WeightConfigError(f"weights must total exactly 1, got {total}")
    return dict(weights)


# ---------------------------------------------------------------------------
# Factor extraction — each a pure function of the candidate
# ---------------------------------------------------------------------------
def economic_value_raw(candidate: OptimizationCandidate) -> Decimal:
    return comparable_value(candidate.economic_effects, candidate.costs)


def _normalize_economic_value(raw: Decimal) -> Decimal:
    if raw <= 0:
        return Decimal(0)
    return _clamp01(raw / ECONOMIC_VALUE_CAP)


def _normalize_time_sensitivity(days: int | None) -> Decimal:
    """Closer hard deadlines rank higher; no deadline is neutral-low."""
    if days is None:
        return Decimal("0.30")
    if days <= 0:
        return Decimal(0)          # already passed — urgency no longer helps
    if days >= 365:
        return Decimal("0.10")
    return _clamp01(Decimal(365 - days) / Decimal(365))


def _normalize_effort(effort_rating: int) -> Decimal:
    """1 (trivial) .. 5 (complex) → 0..1, where 1.0 means maximum effort."""
    clamped = max(1, min(5, effort_rating))
    return (Decimal(clamped - 1) / Decimal(4))


def _normalize_cash_flow(
    candidate: OptimizationCandidate, available_cash: Decimal | None
) -> Decimal:
    required = candidate.liquidity_commitment() + candidate.true_costs()
    if required <= 0:
        return Decimal(0)
    if not available_cash or available_cash <= 0:
        return _ONE
    return _clamp01(required / available_cash)


def compute_score(
    candidate: OptimizationCandidate,
    *,
    weights: dict[ScoreFactor, Decimal] | None = None,
    available_cash: Decimal | None = None,
) -> ScoreBreakdown:
    w = validate_weights(weights or DEFAULT_WEIGHTS)

    confidence_norm = (
        _clamp01(candidate.confidence.overall / Decimal(100))
        if candidate.confidence else Decimal("0.5")
    )
    econ_raw = economic_value_raw(candidate)

    raw_and_norm: dict[ScoreFactor, tuple[Decimal, Decimal]] = {
        ScoreFactor.ECONOMIC_VALUE: (econ_raw, _normalize_economic_value(econ_raw)),
        ScoreFactor.CONFIDENCE: (
            candidate.confidence.overall if candidate.confidence else Decimal(50),
            confidence_norm,
        ),
        ScoreFactor.TIME_SENSITIVITY: (
            Decimal(candidate.days_to_deadline if candidate.days_to_deadline is not None else -1),
            _normalize_time_sensitivity(candidate.days_to_deadline),
        ),
        ScoreFactor.IMPLEMENTATION_EFFORT: (
            Decimal(candidate.effort_rating), _normalize_effort(candidate.effort_rating),
        ),
        ScoreFactor.REQUIRED_CASH_FLOW: (
            candidate.liquidity_commitment() + candidate.true_costs(),
            _normalize_cash_flow(candidate, available_cash),
        ),
        ScoreFactor.USER_RELEVANCE: (
            Decimal(1 if candidate.user_relevant else 0),
            _ONE if candidate.user_relevant else Decimal(0),
        ),
        ScoreFactor.REVERSIBILITY: (
            Decimal(0),
            _REVERSIBILITY_VALUE.get(candidate.reversibility, Decimal("0.60")),
        ),
    }

    components: list[ScoreComponent] = []
    total = Decimal(0)
    for factor_code in sorted(raw_and_norm, key=lambda f: f.value):
        raw, normalized = raw_and_norm[factor_code]
        weight = w[factor_code]
        effective = (_ONE - normalized) if factor_code in NEGATIVE_DIRECTION else normalized
        contribution = (weight * effective).quantize(_SCALE, ROUND_HALF_UP)
        total += contribution
        components.append(ScoreComponent(
            factor_code=factor_code, raw_value=raw.quantize(_SCALE, ROUND_HALF_UP),
            normalized_value=normalized.quantize(_SCALE, ROUND_HALF_UP),
            weight=weight, contribution=contribution,
        ))

    overall = (_clamp01(total) * Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return ScoreBreakdown(overall=overall, components=tuple(components))


def rank(
    candidates: list[OptimizationCandidate],
    *,
    weights: dict[ScoreFactor, Decimal] | None = None,
    available_cash: Decimal | None = None,
) -> list[OptimizationCandidate]:
    """Score and order candidates. Ties break canonically so the order is total.

    Tie-break order: score desc, then `assumption_adjusted_score` desc, then
    economic value desc, then opportunity_code / rule_version_id / candidate_key
    ascending — deterministic regardless of input order.

    The adjusted support score is the SECOND key on purpose: results that differ
    in support but are tied at the DISPLAYED cap still order correctly, without
    the displayed number having to absorb the distinction.
    """
    for candidate in candidates:
        candidate.score = compute_score(
            candidate, weights=weights, available_cash=available_cash
        )

    ordered = sorted(
        candidates,
        key=lambda x: (
            -x.score.overall,
            -_adjusted_support(x),
            -economic_value_raw(x),
            x.opportunity_code,
            x.rule_version_id or "",
            x.candidate_key,
        ),
    )
    for position, candidate in enumerate(ordered, start=1):
        candidate.rank = position
    return ordered


def _adjusted_support(candidate: OptimizationCandidate) -> Decimal:
    """Secondary ordering value: the uncapped, assumption-adjusted support score."""
    if candidate.confidence is None:
        return Decimal(50)
    return candidate.confidence.assumption_adjusted_score


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal(0)
    if value > _ONE:
        return _ONE
    return value
