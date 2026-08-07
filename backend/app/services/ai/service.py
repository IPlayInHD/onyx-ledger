"""AI explanation service (RAG). HARD BOUNDARY: never computes tax.

It loads the user's already-verified analysis figures, retrieves the relevant
published tax rules from pgvector (year-filtered), prompts a provider-agnostic
LLM, validates the answer against the verified numbers (rejecting fabrications),
and persists the turn with citations back to the exact rule versions.
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.database.models import (
    AiConversation,
    AiMessage,
    AiMessageCitation,
    AiPromptContext,
    AnalysisRun,
    Recommendation,
)
from app.integrations.llm import TemplateLlmClient, get_llm_client
from app.services.ai.retrieval import KnowledgeIndex

SYSTEM_PROMPT = (
    "You are Onyx, a Canadian tax educator. You explain already-computed, "
    "verified figures in plain language. You MUST NOT invent or recompute any "
    "dollar amount or rate — only reference the verified numbers provided. "
    "Always ground statements in the cited tax rule. This is educational, not "
    "filing advice."
)


class AiExplanationService:
    """Kept for the guardrail (unit-tested independently)."""

    @staticmethod
    def _validate(answer: str, verified: dict) -> str:
        allowed = {str(Decimal(str(v)).quantize(Decimal("1")))
                   for v in verified.values() if _is_number(v)}
        for m in re.findall(r"\$\s?([\d,]+)", answer):
            if m.replace(",", "") not in allowed:
                return (
                    "I can only explain the figures Onyx has already computed for you. "
                    "Please open your latest analysis to see the exact numbers, and I'll "
                    "explain what they mean."
                )
        return answer


class AiService:
    def __init__(
        self, session: AsyncSession, llm: TemplateLlmClient | None = None
    ) -> None:
        self.s = session
        self.llm = llm or get_llm_client()
        self.index = KnowledgeIndex(session)

    async def ask(
        self, user_id: uuid.UUID, question: str, tax_year: int,
        conversation_id: uuid.UUID | None = None,
    ) -> dict:
        conv = await self._conversation(user_id, conversation_id, question)
        self.s.add(AiMessage(conversation_id=conv.id, role="user", content=question))
        await self.s.flush()

        verified, analysis_id = await self._verified_context(user_id, tax_year)

        # Retrieve grounding chunks (lazy-index the year's rules on first use).
        if not await self.index.count(tax_year):
            await self.index.reindex_published_rules(tax_year)
        chunks = await self.index.retrieve(question, tax_year)

        prompt = self._build_prompt(question, verified, chunks)
        raw = await self.llm.complete(SYSTEM_PROMPT, prompt)
        answer = AiExplanationService._validate(raw, verified)

        msg = AiMessage(
            conversation_id=conv.id, role="assistant", content=answer,
            model=getattr(self.llm, "model", None),
            confidence_score=90 if chunks else 60,
        )
        self.s.add(msg)
        await self.s.flush()
        self.s.add(AiPromptContext(message_id=msg.id, context={
            "verified": {k: str(v) for k, v in verified.items()},
            "chunks": [{"rule_version_id": str(c["rule_version_id"]),
                        "distance": c["distance"]} for c in chunks],
        }))
        citations = []
        for c in chunks:
            self.s.add(AiMessageCitation(
                message_id=msg.id, tax_rule_version_id=c["rule_version_id"],
                analysis_id=analysis_id,
            ))
            citations.append(str(c["rule_version_id"]))
        await self.s.flush()

        return {
            "conversation_id": str(conv.id),
            "message_id": str(msg.id),
            "answer": answer,
            "citations": citations,
            "grounded": bool(chunks),
        }

    async def _conversation(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        question: str,
    ) -> AiConversation:
        if conversation_id is not None:
            conv = await self.s.get(AiConversation, conversation_id)
            if not conv or conv.user_id != user_id:
                raise NotFound("Conversation not found")
            return conv
        conv = AiConversation(user_id=user_id, title=(question or "Conversation")[:60])
        self.s.add(conv)
        await self.s.flush()
        return conv

    async def _verified_context(
        self, user_id: uuid.UUID, tax_year: int
    ) -> tuple[dict[str, Decimal | str | None], uuid.UUID | None]:
        run = await self.s.scalar(
            select(AnalysisRun).where(
                AnalysisRun.user_id == user_id, AnalysisRun.tax_year == tax_year,
                AnalysisRun.status == "completed",
            ).order_by(AnalysisRun.created_at.desc())
        )
        if not run:
            return {}, None
        # Figures and opportunity titles share this map; annotated so the string
        # titles added below are part of the declared shape rather than a silent
        # widening of a Decimal-only dict.
        verified: dict[str, Decimal | str | None] = {
            "taxable_income": run.taxable_income,
            "estimated_tax": run.estimated_tax,
            "estimated_savings": run.estimated_savings,
            "marginal_rate": run.marginal_rate,
        }
        recs = await self.s.scalars(
            select(Recommendation).where(Recommendation.analysis_id == run.id)
            .order_by(Recommendation.priority).limit(5)
        )
        for i, rec in enumerate(recs):
            verified[f"opportunity_{i+1}"] = rec.title
        return {k: v for k, v in verified.items() if v is not None}, run.id

    @staticmethod
    def _build_prompt(question: str, verified: dict, chunks: list[dict]) -> str:
        vlines = "\n".join(f"- {k}: {v}" for k, v in verified.items()) or "- (no analysis run yet)"
        rlines = "\n".join(f"- {c['content']}" for c in chunks) or "- (no matching rules)"
        return (
            f"Verified figures for this user:\n{vlines}\n\n"
            f"Relevant tax rules:\n{rlines}\n\nUser question: {question}"
        )


def _is_number(v: object) -> bool:
    try:
        Decimal(str(v))
        return True
    except Exception:  # noqa: BLE001
        return False
