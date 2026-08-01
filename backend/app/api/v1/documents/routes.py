from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.database.models import Document, ExtractionField
from app.services.document_processing.service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])


class UploadRequest(BaseModel):
    document_type_code: str
    filename: str
    mime_type: str | None = None
    tax_year: int | None = None


class ProcessRequest(BaseModel):
    # dev/demo: pass structured `fields` or OCR `text`. Production: a worker OCRs
    # the object in storage and calls the same service.
    text: str | None = None
    fields: dict | None = None


class ConfirmRequest(BaseModel):
    tax_year: int


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_upload(
    body: UploadRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    doc, presigned = await DocumentService(session).create_upload(
        user_id, body.document_type_code, body.filename, body.mime_type, body.tax_year
    )
    return {"document_id": str(doc.id), "status": doc.status, "upload_url": presigned}


@router.post("/{document_id}/process")
async def process(
    document_id: uuid.UUID, body: ProcessRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    ex = await DocumentService(session).process(document_id, text=body.text, fields=body.fields)
    fields = await session.scalars(
        select(ExtractionField).where(ExtractionField.extraction_id == ex.id)
    )
    return {
        "extraction_id": str(ex.id), "status": ex.status, "confidence": float(ex.confidence or 0),
        "fields": [{"name": f.field_name, "value": str(f.value_number)} for f in fields],
    }


@router.post("/{document_id}/confirm")
async def confirm(
    document_id: uuid.UUID, body: ConfirmRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    created = await DocumentService(session).confirm(user_id, document_id, body.tax_year)
    return {"created": created}


@router.get("")
async def list_documents(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[dict]:
    rows = await session.scalars(
        select(Document).where(Document.user_id == user_id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
    )
    return [{"id": str(d.id), "status": d.status, "object_key": d.object_key} for d in rows]
