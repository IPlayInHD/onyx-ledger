from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, created_at_col, updated_at_col, uuid_pk

EMBEDDING_DIM = 1536


class AiConversation(Base):
    __tablename__ = "ai_conversation"
    __table_args__ = {"schema": "ai"}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE")
    )
    title: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    deleted_at: Mapped[datetime | None] = mapped_column()


class AiMessage(Base):
    __tablename__ = "ai_message"
    __table_args__ = {"schema": "ai"}

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai.ai_conversation.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String)
    token_count: Mapped[int | None] = mapped_column(Integer)
    confidence_score: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = created_at_col()


class AiMessageCitation(Base):
    __tablename__ = "ai_message_citation"
    __table_args__ = {"schema": "ai"}

    id: Mapped[uuid.UUID] = uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai.ai_message.id", ondelete="CASCADE")
    )
    tax_rule_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    recommendation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class AiPromptContext(Base):
    __tablename__ = "ai_prompt_context"
    __table_args__ = {"schema": "ai"}

    id: Mapped[uuid.UUID] = uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai.ai_message.id", ondelete="CASCADE")
    )
    context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class KnowledgeEmbedding(Base):
    __tablename__ = "knowledge_embedding"
    __table_args__ = {"schema": "ai"}

    id: Mapped[uuid.UUID] = uuid_pk()
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tax_year: Mapped[int | None] = mapped_column()
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = created_at_col()
