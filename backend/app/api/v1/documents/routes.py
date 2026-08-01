from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import current_user_id
from app.core.exceptions import DomainError

router = APIRouter(prefix="/documents", tags=["documents"])


class NotImplementedYet(DomainError):
    status_code = 501
    error_type = "https://onyx.ledger/errors/not-implemented"
    title = "Not Implemented"


@router.post("")
async def upload(_user=Depends(current_user_id)) -> dict:
    # Scaffolded: presigned S3 upload → docs.document → Celery OCR pipeline.
    raise NotImplementedYet("Document upload pipeline is scaffolded; S3/OCR adapters pending.")
