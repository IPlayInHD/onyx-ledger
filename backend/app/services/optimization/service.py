"""Optimization engine — ranks opportunities with an explainable score.

score = w1·impact + w2·confidence + w3·relevance − w4·complexity + w5·actionability
Weights are configuration; inputs are persisted on reco.recommendation so the
ordering is reproducible.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.tax_engine.rules_service import Opportunity

WEIGHTS = {"impact": 0.45, "confidence": 0.2, "relevance": 0.15,
           "complexity": 0.1, "actionability": 0.1}


def _norm_impact(impact: Decimal | None) -> float:
    if not impact or impact <= 0:
        return 0.0
    # log-ish normalization: $0→0, ~$5k+→1
    return min(float(impact) / 5000.0, 1.0)


def score(opp: Opportunity) -> float:
    impact = _norm_impact(opp.estimated_impact)
    confidence = 0.9 if opp.estimated_impact is not None else 0.6
    relevance = 1.0
    complexity = 0.3
    actionability = 0.8
    return (
        WEIGHTS["impact"] * impact
        + WEIGHTS["confidence"] * confidence
        + WEIGHTS["relevance"] * relevance
        - WEIGHTS["complexity"] * complexity
        + WEIGHTS["actionability"] * actionability
    )


def rank(opportunities: list[Opportunity]) -> list[tuple[Opportunity, int, int]]:
    """Return (opportunity, confidence_score_0_100, priority) ordered best-first."""
    scored = sorted(opportunities, key=score, reverse=True)
    out = []
    for opp in scored:
        confidence = 90 if opp.estimated_impact is not None else 60
        out.append((opp, confidence, opp.priority))
    return out
