"""Explanation orchestration: assemble → provider → validate → fallback.

The failure posture is the whole point of this module. A configured provider
is an ENHANCEMENT: whatever it does — errors, times out, returns malformed
output, or returns fluent text that contradicts its input — the deterministic
renderer serves the request. Materially unsafe model output is REJECTED whole,
never repaired, and the rejection reasons are logged as closed machine codes.

The deterministic renderer's own output goes through the SAME validators as a
self-check on every request; it failing is a programming defect and raises
rather than shipping an invalid explanation.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.explanation import ExplanationInputV1, ExplanationOutputV1
from app.services.ai.explanation.assembler import ExplanationInputAssembler
from app.services.ai.explanation.renderer import (
    DeterministicExplanationRenderer,
    ExplanationProvider,
    get_explanation_provider,
)
from app.services.ai.explanation.validators import (
    SCHEMA_INVALID,
    ExplanationViolation,
    validate_explanation,
)

logger = logging.getLogger(__name__)


class ExplanationRenderDefect(RuntimeError):
    """The deterministic renderer produced output its own validators reject.

    Never a user's fault and never model behavior — a bug in this package.
    Raised so it fails loudly in tests and monitoring instead of shipping an
    invalid explanation."""


class ExplanationService:
    def __init__(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        provider: ExplanationProvider | None = None,
    ):
        self.s = session
        self.user_id = user_id
        #: Explicit injection is for tests; production resolves the seam.
        self.provider = provider if provider is not None else get_explanation_provider()
        self.renderer = DeterministicExplanationRenderer()

    async def explain(
        self,
        explanation_type: str,
        *,
        subject_id: str | None,
        tax_year: int | None,
        as_of: date | None = None,
    ) -> tuple[ExplanationOutputV1, Literal["model", "fallback"], ExplanationInputV1]:
        """Returns (validated output, renderer_mode, assembled input)."""
        started = time.monotonic()
        assembled = await ExplanationInputAssembler(self.s, self.user_id).assemble(
            explanation_type,
            subject_id=subject_id,
            tax_year=tax_year,
            as_of=as_of,
        )

        candidate: ExplanationOutputV1 | None = None
        mode: Literal["model", "fallback"] = "fallback"
        rejection_codes: list[str] = []
        if self.provider is not None:
            candidate, rejection_codes = await self._model_candidate(assembled)
            if candidate is not None:
                mode = "model"

        if candidate is None:
            candidate = self.renderer.render(assembled)
            self_check = validate_explanation(assembled, candidate)
            if self_check:
                raise ExplanationRenderDefect(
                    "deterministic renderer failed its own validators: "
                    + ", ".join(sorted({v.code for v in self_check}))
                )

        # Privacy-safe operational metadata only: types, versions, codes,
        # timing. No tax payloads, no user-authored text, no binding hashes.
        logger.info(
            "explanation rendered",
            extra={
                "explanation_type": assembled.explanation_type,
                "subject_type": assembled.subject_binding.subject_type,
                "input_contract_version": assembled.explanation_input_version,
                "output_contract_version": candidate.explanation_output_version,
                "renderer_mode": mode,
                "provider_configured": self.provider is not None,
                "validation_rejections": sorted(set(rejection_codes)),
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return candidate, mode, assembled

    async def _model_candidate(
        self, assembled: ExplanationInputV1
    ) -> tuple[ExplanationOutputV1 | None, list[str]]:
        """One provider attempt: any failure of any kind means no candidate."""
        assert self.provider is not None
        try:
            raw = await self.provider.generate(assembled)
        except Exception:  # noqa: BLE001 — provider failure must never 500
            logger.warning("explanation provider failed; using fallback",
                           exc_info=True)
            return None, ["PROVIDER_ERROR"]
        try:
            candidate = (raw if isinstance(raw, ExplanationOutputV1)
                         else ExplanationOutputV1.model_validate(raw))
        except ValidationError:
            return None, [SCHEMA_INVALID]
        violations: list[ExplanationViolation] = validate_explanation(
            assembled, candidate
        )
        if violations:
            logger.info(
                "model explanation rejected",
                extra={
                    "explanation_type": assembled.explanation_type,
                    "rejection_codes": sorted({v.code for v in violations}),
                },
            )
            return None, [v.code for v in violations]
        return candidate, []


__all__ = ["ExplanationRenderDefect", "ExplanationService"]
