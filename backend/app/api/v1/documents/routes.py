from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.core.exceptions import Conflict, ValidationError
from app.database.models import Document, ExtractionField
from app.services.admission import OperationClass, admission_guard
from app.services.admission.guard import owned_dedupe_key, user_scope
from app.services.admission.limits import (
    ALLOWED_DOCUMENT_MIME_TYPES,
    MAX_DOCUMENT_BYTES,
    MAX_EXTRACTION_FIELDS,
    MAX_EXTRACTION_TEXT_BYTES,
    MAX_FILENAME_LENGTH,
)
from app.services.document_processing.service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])


class UploadRequest(BaseModel):
    """Bounded at the schema, so an oversized field is refused by the parser
    before a handler ever builds a string out of it."""

    document_type_code: str = Field(max_length=64)
    filename: str = Field(max_length=MAX_FILENAME_LENGTH)
    mime_type: str | None = Field(default=None, max_length=255)
    tax_year: int | None = Field(default=None, ge=1900, le=2200)
    #: Declared size, used to refuse an oversized upload BEFORE a presigned URL
    #: is issued. It is a claim, not proof — see the note on the handler.
    byte_size: int | None = Field(default=None, ge=0)


class ProcessRequest(BaseModel):
    # dev/demo: pass structured `fields` or OCR `text`. Production: a worker OCRs
    # the object in storage and calls the same service.
    text: str | None = Field(default=None, max_length=MAX_EXTRACTION_TEXT_BYTES)
    fields: dict | None = None

    @field_validator("text")
    @classmethod
    def _text_within_byte_bound(cls, value: str | None) -> str | None:
        """`max_length` counts CHARACTERS; the bound is named in bytes.

        A megabyte-long limit expressed as a character count admits four
        megabytes of UTF-8, and the regex extraction that runs over it scales
        with the encoded size. Checked here as well as declared above, so the
        name and the enforcement agree.
        """
        if value is not None and len(value.encode("utf-8")) > MAX_EXTRACTION_TEXT_BYTES:
            raise ValueError(
                f"extraction text exceeds the {MAX_EXTRACTION_TEXT_BYTES} byte maximum"
            )
        return value


class ConfirmRequest(BaseModel):
    tax_year: int


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_upload(
    body: UploadRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """Register a document and hand back a presigned upload URL.

    TWO size checks, and only the second one is a bound.

    The bytes never pass through this process — they go straight to object
    storage — so the check here is on the DECLARED size. It is an early, cheap
    refusal that catches an honest client and saves a round trip. It is NOT
    enforcement: a caller that wants to store 30 MB under a 25 MB limit simply
    declares 1 MB.

    Enforcement is the ceiling attached to the presigned authorization itself,
    which the store applies to the bytes that actually arrive
    (`ObjectStorage.presign_put(..., max_bytes=...)`; in S3 that is a POST
    policy `content-length-range` condition). The declaration is honoured too,
    where it is tighter — a client that says 1 MB and sends 5 MB is refused
    even though 5 MB is under the platform limit.
    """
    if body.mime_type and body.mime_type.split(";")[0].strip().lower() \
            not in ALLOWED_DOCUMENT_MIME_TYPES:
        # An allow-list: a deny-list admits every format nobody thought of.
        raise ValidationError(
            f"unsupported content type; allowed: "
            f"{', '.join(sorted(ALLOWED_DOCUMENT_MIME_TYPES))}"
        )
    if body.byte_size is not None and body.byte_size > MAX_DOCUMENT_BYTES:
        raise ValidationError(
            f"document exceeds the {MAX_DOCUMENT_BYTES} byte maximum"
        )

    async with admission_guard(
        OperationClass.DOCUMENT_UPLOAD, scope_id=user_scope(user_id)
    ):
        doc, presigned = await DocumentService(session).create_upload(
            user_id, body.document_type_code, body.filename,
            body.mime_type, body.tax_year, declared_bytes=body.byte_size,
        )
        return {
            "document_id": str(doc.id), "status": doc.status,
            "upload_url": presigned,
        }


@router.post("/{document_id}/process")
async def process(
    document_id: uuid.UUID, body: ProcessRequest,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """Extract fields from an uploaded document.

    Uploading and PROCESSING are separate costs, and this is the expensive one:
    it is the classic path for turning one cheap request into minutes of CPU.
    Bounded concurrency, and a dedupe key on the document so re-processing the
    same immutable document cannot create unlimited work.
    """
    if body.fields is not None and len(body.fields) > MAX_EXTRACTION_FIELDS:
        raise ValidationError(
            f"at most {MAX_EXTRACTION_FIELDS} extraction fields per request"
        )

    async with admission_guard(
        OperationClass.DOCUMENT_PROCESS,
        scope_id=user_scope(user_id),
        dedupe_key=owned_dedupe_key(user_id, "process", str(document_id)),
    ) as ticket:
        if ticket.duplicate_of_active:
            # The dedupe key found an extraction already in flight for this
            # document. Phase 1 computed the key and then ran the body anyway,
            # which made it decorative: twenty retries against one document
            # produced twenty concurrent extractions and twenty rows, and the
            # last one to finish silently won.
            raise Conflict(
                "an extraction for this document is already running; "
                "wait for it to finish"
            )
        ex = await DocumentService(session).process(
            document_id, text=body.text, fields=body.fields)
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
    """Turn a confirmed extraction into income/expense rows.

    NORMAL_WRITE rather than DOCUMENT_PROCESS: no extraction runs here, but each
    call writes a row per extracted field plus a provenance link, so repeated
    confirmation of one document is an unbounded row creator.
    """
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        created = await DocumentService(session).confirm(user_id, document_id, body.tax_year)
        return {"created": created}


@router.delete("/{document_id}", status_code=status.HTTP_200_OK)
async def delete_document(
    document_id: uuid.UUID,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """Delete one owned document: binary, extraction and all.

    The document is named by ID and the object key is resolved SERVER-SIDE from
    the owned row. A client never supplies a bucket or a key — a caller able to
    name its own key could ask the platform to delete an arbitrary object,
    including another tenant's, and the ownership check would never see it.

    Synchronous, and deliberately so. The work is one object deletion and two
    bounded statements, all of which comfortably fit inside a request; a 202
    with a background job would add a durable queue, a worker and a state
    machine to defer something that already finished. If object deletion ever
    becomes slow enough to matter — a versioned bucket needing per-version
    deletes — this returns 202 and grows the job then, on evidence.

    NORMAL_WRITE admission: cheap per call, but it is a mutation and an
    unbounded caller should not be able to drive object-store deletions in a
    loop for free.
    """
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        outcome = await DocumentService(session).delete_document(user_id, document_id)
        # Minimal by design (§36): no object key, no bucket, no provider
        # response, no internal phase. "deleted" is true whether this call did
        # the work or found it already done — the caller asked for a state, not
        # for a history.
        return {"status": "deleted", "already_deleted": outcome.already_deleted}


@router.get("")
async def list_documents(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[dict]:
    rows = await session.scalars(
        select(Document).where(Document.user_id == user_id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
    )
    # The object key is deliberately NOT returned. It is infrastructure, it is
    # not authorization, and until Entry 11B4 it carried the user's filename
    # straight into every client that listed their documents (PD-2). A caller
    # identifies a document by its id; the key is resolved server-side.
    return [{"id": str(d.id), "status": d.status} for d in rows]
