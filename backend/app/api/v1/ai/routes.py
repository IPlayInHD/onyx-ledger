from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.core.exceptions import NotFound
from app.database.models import AiConversation, AiMessage
from app.schemas.explanation import ExplanationEnvelopeOut, ExplanationType
from app.services.admission import OperationClass, admission_guard
from app.services.admission.guard import user_scope
from app.services.ai.explanation import ExplanationService
from app.services.ai.service import AiService

router = APIRouter(prefix="/ai", tags=["ai"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    tax_year: int = Field(ge=1900, le=2200)


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """Open an empty conversation.

    One INSERT, so it is a NORMAL_WRITE rather than an AI_EXPLAIN — but it is
    still an unbounded row creator, and a loop against it costs the platform
    storage without ever touching the expensive path.
    """
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        conv = AiConversation(user_id=user_id, title="New conversation")
        session.add(conv)
        await session.flush()
        return {"id": str(conv.id), "title": conv.title}


@router.post("/conversations/{conversation_id}/messages")
async def ask(
    conversation_id: uuid.UUID, body: AskRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """Ask within an existing conversation.

    AI_EXPLAIN, because the work is retrieval plus a completion: a vector search
    over the year's published rules, a lazy REINDEX of that year on first use,
    and a provider call whose cost lives outside this process. That last part is
    exactly why the ceiling has to be here — nothing downstream of this handler
    is in a position to refuse.
    """
    async with admission_guard(
        OperationClass.AI_EXPLAIN, scope_id=user_scope(user_id)
    ):
        return await AiService(session).ask(
            user_id, body.question, body.tax_year, conversation_id=conversation_id
        )


@router.post("/ask", status_code=status.HTTP_201_CREATED)
async def ask_new(
    body: AskRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """One-shot: starts a new conversation and answers."""
    async with admission_guard(
        OperationClass.AI_EXPLAIN, scope_id=user_scope(user_id)
    ):
        return await AiService(session).ask(user_id, body.question, body.tax_year)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[dict]:
    conv = await session.get(AiConversation, conversation_id)
    if not conv or conv.user_id != user_id:
        raise NotFound("Conversation not found")
    rows = await session.scalars(
        select(AiMessage).where(AiMessage.conversation_id == conversation_id)
        .order_by(AiMessage.created_at)
    )
    return [{"role": m.role, "content": m.content, "confidence": m.confidence_score} for m in rows]


class ExplanationRequest(BaseModel):
    """What to explain. No tax facts can be supplied here — the assembler
    loads the authenticated user's authoritative subject through the owning
    tenant-isolated services, and the renderer sees only what they return."""

    model_config = ConfigDict(extra="forbid")

    explanation_type: ExplanationType
    subject_id: str | None = Field(
        None, max_length=200,
        description="The authoritative subject: analysis id (TAX_POSITION), "
                    "opportunity source id or code (OPPORTUNITY), optimization "
                    "run id (PORTFOLIO), scenario id (SCENARIO, COMPARISON), "
                    "decision-journal id (EVIDENCE_READINESS). Unused for "
                    "WHAT_CHANGED.")
    tax_year: int | None = Field(
        None, ge=2000, le=2100,
        description="Required for OPPORTUNITY and WHAT_CHANGED.")


@router.post(
    "/explanations",
    response_model=ExplanationEnvelopeOut,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a validated explanation of a governed result",
    description=(
        "Renders the caller's own sealed/governed result as a customer-safe "
        "explanation. The renderer holds no authority: every number, verdict, "
        "citation, deadline and assumption comes from the assembled governed "
        "input, and generated output survives only if the deterministic "
        "validators accept it — otherwise the deterministic template renderer "
        "answers. No internal hashes are exposed."
    ),
)
async def create_explanation(
    body: ExplanationRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> ExplanationEnvelopeOut:
    async with admission_guard(
        OperationClass.AI_EXPLAIN, scope_id=user_scope(user_id)
    ):
        output, mode, assembled = await ExplanationService(session, user_id).explain(
            body.explanation_type,
            subject_id=body.subject_id,
            tax_year=body.tax_year,
        )
        return ExplanationEnvelopeOut(
            explanation=output,
            explanation_type=assembled.explanation_type,
            renderer_mode=mode,
            as_of=assembled.as_of,
        )
