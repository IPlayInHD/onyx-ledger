from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.core.exceptions import NotFound
from app.database.models import AiConversation, AiMessage
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
