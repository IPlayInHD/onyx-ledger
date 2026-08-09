"""Document intelligence pipeline: upload -> (OCR) -> extract -> validate ->
confirm -> financial-profile update. Bytes never transit the API or Postgres;
only object-storage references + extracted fields are stored.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

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
from app.services.admission.limits import MAX_DOCUMENT_BYTES
from app.services.document_processing.ocr import FIELD_FACT, FIELD_TARGET, extract_fields
from app.services.ioe.freshness_producers import (
    on_document_status_changed,
    on_financial_data_changed,
)

LOW_CONFIDENCE = 0.75

#: The object-key version. Present so a future format change is distinguishable
#: from the legacy filename-bearing keys without parsing them, and so
#: `test_pd2_object_keys` can assert on the shape rather than on a regex over
#: everything that has ever existed.
OBJECT_KEY_VERSION = "v2"


def _opaque_object_key(user_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """A server-authoritative document key that carries no user content.

    Every component is an internal identifier this system generated:

        {user_id}/v2/{document_id}

    NO filename, no extension, no title, no tax-year description. The MIME type
    lives in `docs.document.mime_type`, which is the right place for it — a
    content type in a path is decoration, and the moment an extension is
    allowed the argument for allowing the stem follows.

    Deriving the key from the document's OWN id rather than a fresh uuid4 means
    the authoritative pointer and the row identify each other: given a row the
    key is recomputable, and given a key the row is findable. That is what lets
    the orphan check in §39 compare the two sets at all.
    """
    return f"{user_id}/{OBJECT_KEY_VERSION}/{document_id}"


class DocumentService:
    def __init__(self, session: AsyncSession):
        self.s = session
        self.settings = get_settings()
        self.storage = get_object_storage()

    async def create_upload(
        self, user_id: uuid.UUID, doc_type_code: str, filename: str,
        mime_type: str | None = None, tax_year: int | None = None,
        declared_bytes: int | None = None,
    ) -> tuple[Document, str]:
        """Register a document and authorize ONE bounded upload.

        The authorization carries the TIGHTER of the platform maximum and
        whatever the client said it was about to send. Honouring the
        declaration is free and strictly better: a client that says 1 MB and
        then sends 5 MB is refused even though 5 MB is under the platform
        limit, and a client that lies downward gains nothing by it.
        """
        dtype = await self.s.scalar(select(DocumentType).where(DocumentType.code == doc_type_code))
        if not dtype:
            raise ValidationError(f"Unknown document type '{doc_type_code}'")
        bucket = self.settings.s3_bucket_documents
        doc = Document(
            user_id=user_id, document_type_id=dtype.id, tax_year=tax_year,
            bucket=bucket, object_key="", mime_type=mime_type, status="uploaded",
        )
        self.s.add(doc)
        await self.s.flush()
        # PD-2: the key USED to be f"{user_id}/{uuid4()}/{filename}".
        #
        # A filename is user free text and routinely contains a person's name,
        # their employer, or what the document is about — "Ali Abbas 2025 T4
        # medical.pdf". Object keys surface in bucket listings, CDN and access
        # logs, support tooling and provider consoles: places with entirely
        # different access control from the RLS-protected row, and none of them
        # reached by a deletion. The key is server-generated and opaque now.
        #
        # `filename` is still accepted and still validated by the route, and is
        # then deliberately NOT stored anywhere — see `_opaque_object_key`.
        #
        # The `{user_id}/` prefix stays, on Entry 11A's recommendation: it is an
        # internal UUID rather than a name, and it makes account-level purge of
        # object storage a single prefix operation instead of a row-by-row walk.
        doc.object_key = _opaque_object_key(user_id, doc.id)
        await self.s.flush()
        presigned = self.storage.presign_put(
            bucket, doc.object_key, mime_type or "application/octet-stream",
            max_bytes=min(declared_bytes or MAX_DOCUMENT_BYTES, MAX_DOCUMENT_BYTES),
        )
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
            extracted_at=datetime.now(tz=UTC),
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
            # Distinct names per branch: one `row` reused across both made an
            # expense row and an income row interchangeable to a reader and to
            # the type checker, and the link below is keyed on which one it is.
            if kind == "income" and code in income_types:
                income_row = IncomeSource(
                    user_id=user_id, tax_year=tax_year, income_type_id=income_types[code],
                    amount=field.value_number, verification_status="document_backed",
                )
                self.s.add(income_row)
                await self.s.flush()
                self.s.add(DocumentLink(
                    document_id=doc.id, income_source_id=income_row.id,
                    income_tax_year=tax_year,
                ))
                created["income"] += 1
            elif kind == "expense" and code in categories:
                expense_row = ExpenseRecord(
                    user_id=user_id, tax_year=tax_year, expense_category_id=categories[code],
                    amount=field.value_number, verification_status="document_backed",
                )
                self.s.add(expense_row)
                await self.s.flush()
                self.s.add(DocumentLink(
                    document_id=doc.id, expense_record_id=expense_row.id,
                    expense_tax_year=tax_year,
                ))
                created["expense"] += 1

        # `confirmed` is NOT in the document_status CHECK constraint — the
        # stored status stays `processed`. Confirmation is an event about the
        # evidence, not a new storage state, and inventing a status value here
        # would need a migration for no gain.
        doc.status = "processed"
        await self.s.flush()

        # Confirming an extraction is where a document becomes a CALCULATION
        # INPUT: the rows created above are income and expense the engine will
        # read on the next analysis. That is a financial-data change, not merely
        # a document-status change, so it carries the financial reason.
        #
        # Emitted only when something was actually created. A confirm that
        # produced no rows changed no input and must not invalidate anything.
        if created["income"] or created["expense"]:
            await on_financial_data_changed(
                self.s, user_id, tax_year,
                change_token=f"document_confirm:{document_id}")
        # The evidence behind an eligibility determination also moved, which is
        # a different fact with its own reason.
        await on_document_status_changed(self.s, user_id, document_id, "confirmed")
        return created
