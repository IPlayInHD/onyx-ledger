"""Document intelligence pipeline: upload -> (OCR) -> extract -> validate ->
confirm -> financial-profile update. Bytes never transit the API or Postgres;
only object-storage references + extracted fields are stored.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFound, ValidationError
from app.database.models import (
    Document,
    DocumentExtraction,
    DocumentLink,
    DocumentType,
    ExpenseCategory,
    ExpenseRecord,
    ExtractionField,
    IncomeSource,
    IncomeType,
)
from app.integrations.storage import get_object_storage
from app.services.document_processing.ocr import FIELD_FACT, FIELD_TARGET, extract_fields

LOW_CONFIDENCE = 0.75


class DocumentService:
    def __init__(self, session: AsyncSession):
        self.s = session
        self.settings = get_settings()
        self.storage = get_object_storage()

    async def create_upload(
        self, user_id: uuid.UUID, doc_type_code: str, filename: str,
        mime_type: str | None = None, tax_year: int | None = None,
    ) -> tuple[Document, str]:
        dtype = await self.s.scalar(select(DocumentType).where(DocumentType.code == doc_type_code))
        if not dtype:
            raise ValidationError(f"Unknown document type '{doc_type_code}'")
        bucket = self.settings.s3_bucket_documents
        key = f"{user_id}/{uuid.uuid4()}/{filename}"
        doc = Document(
            user_id=user_id, document_type_id=dtype.id, tax_year=tax_year,
            bucket=bucket, object_key=key, mime_type=mime_type, status="uploaded",
        )
        self.s.add(doc)
        await self.s.flush()
        presigned = self.storage.presign_put(bucket, key, mime_type or "application/octet-stream")
        return doc, presigned

    async def process(
        self, document_id: uuid.UUID, *, text: str | None = None, fields: dict | None = None,
    ) -> DocumentExtraction:
        """Run extraction (structured or OCR-text) and persist the fields."""
        doc = await self.s.get(Document, document_id)
        if not doc:
            raise NotFound("Document not found")
        dtype = await self.s.get(DocumentType, doc.document_type_id) if doc.document_type_id else None
        code = dtype.code if dtype else ""

        doc.status = "processing"
        extracted, confidence = extract_fields(code, text=text, fields=fields)
        extraction = DocumentExtraction(
            document_id=doc.id,
            engine="structured" if fields else "ocr-regex",
            status="processed" if confidence >= LOW_CONFIDENCE else "needs_review",
            confidence=round(confidence, 4),
            extracted_at=datetime.now(tz=timezone.utc),
        )
        self.s.add(extraction)
        await self.s.flush()
        for name, value in extracted.items():
            self.s.add(ExtractionField(
                extraction_id=extraction.id, field_name=name,
                fact_key=FIELD_FACT.get(name), value_number=value, confidence=confidence,
            ))
        if text is not None and doc.content_hash is None:
            doc.content_hash = hashlib.sha256(text.encode()).hexdigest()
        doc.status = "processed" if confidence >= LOW_CONFIDENCE else "processed"
        await self.s.flush()
        return extraction

    async def confirm(self, user_id: uuid.UUID, document_id: uuid.UUID, tax_year: int) -> dict:
        """Turn the latest extraction into income/expense rows + provenance links."""
        doc = await self.s.get(Document, document_id)
        if not doc or doc.user_id != user_id:
            raise NotFound("Document not found")
        extraction = await self.s.scalar(
            select(DocumentExtraction).where(DocumentExtraction.document_id == document_id)
            .order_by(DocumentExtraction.created_at.desc())
        )
        if not extraction:
            raise ValidationError("No extraction to confirm; process the document first")

        income_types = {t.code: t.id for t in await self.s.scalars(select(IncomeType))}
        categories = {c.code: c.id for c in await self.s.scalars(select(ExpenseCategory))}
        created = {"income": 0, "expense": 0}

        for field in await self.s.scalars(
            select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)
        ):
            target = FIELD_TARGET.get(field.field_name)
            if not target or field.value_number is None:
                continue
            kind, code = target
            if kind == "income" and code in income_types:
                row = IncomeSource(
                    user_id=user_id, tax_year=tax_year, income_type_id=income_types[code],
                    amount=field.value_number, verification_status="document_backed",
                )
                self.s.add(row)
                await self.s.flush()
                self.s.add(DocumentLink(
                    document_id=doc.id, income_source_id=row.id, income_tax_year=tax_year
                ))
                created["income"] += 1
            elif kind == "expense" and code in categories:
                row = ExpenseRecord(
                    user_id=user_id, tax_year=tax_year, expense_category_id=categories[code],
                    amount=field.value_number, verification_status="document_backed",
                )
                self.s.add(row)
                await self.s.flush()
                self.s.add(DocumentLink(
                    document_id=doc.id, expense_record_id=row.id, expense_tax_year=tax_year
                ))
                created["expense"] += 1

        doc.status = "processed"
        await self.s.flush()
        return created
