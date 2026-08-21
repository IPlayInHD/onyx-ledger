"""AI explanation layer — a validated, fail-safe renderer over certified
structured output.

The model (when one is configured) holds no authority here: it receives
`ExplanationInputV1`, may only restate what that input supplies, and its
candidate output survives only if the deterministic validators accept it.
Every rejection falls back to the deterministic template renderer, so the
product is fully functional with zero model calls.
"""
from app.services.ai.explanation.service import ExplanationService

__all__ = ["ExplanationService"]
