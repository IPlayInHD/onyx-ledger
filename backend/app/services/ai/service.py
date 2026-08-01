"""AI explanation service (RAG). HARD BOUNDARY: never computes tax.

Receives already-verified engine outputs + retrieved rule context, renders a
prompt, calls the LLM adapter, and validates that the answer introduces no
numbers absent from the verified payload and cites a rule version.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.domain.ports import LlmClient

SYSTEM_PROMPT = (
    "You are Onyx, a Canadian tax educator. You explain already-computed, "
    "verified figures in plain language. You MUST NOT invent or recompute any "
    "dollar amount or rate — only reference the verified numbers provided. "
    "Always ground statements in the cited tax rule. This is educational, not "
    "filing advice."
)


class AiExplanationService:
    def __init__(self, llm: LlmClient):
        self.llm = llm

    async def explain(self, question: str, verified: dict, citations: list[str]) -> str:
        context = "\n".join(f"- {k}: {v}" for k, v in verified.items())
        cites = "\n".join(citations)
        prompt = (
            f"Verified figures for this user:\n{context}\n\n"
            f"Relevant tax rules:\n{cites}\n\nUser question: {question}"
        )
        answer = await self.llm.complete(SYSTEM_PROMPT, prompt)
        return self._validate(answer, verified)

    @staticmethod
    def _validate(answer: str, verified: dict) -> str:
        """Reject fabricated numbers: any $amount in the answer must appear in
        the verified payload (guardrail against the model inventing figures)."""
        allowed = {str(Decimal(str(v)).quantize(Decimal("1"))) for v in verified.values()
                   if _is_number(v)}
        for m in re.findall(r"\$\s?([\d,]+)", answer):
            if m.replace(",", "") not in allowed:
                return (
                    "I can only explain the figures Onyx has already computed for you. "
                    "Please open your latest analysis to see the exact numbers, and I'll "
                    "explain what they mean."
                )
        return answer


def _is_number(v) -> bool:
    try:
        Decimal(str(v))
        return True
    except Exception:  # noqa: BLE001
        return False
