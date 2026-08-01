from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import current_user_id
from app.core.exceptions import DomainError

router = APIRouter(prefix="/admin", tags=["admin"])


class NotImplementedYet(DomainError):
    status_code = 501
    error_type = "https://onyx.ledger/errors/not-implemented"
    title = "Not Implemented"


@router.post("/ingestion/jobs")
async def create_ingestion_job(_user=Depends(current_user_id)) -> dict:
    # Scaffolded: Raw→Extract→Validate→Transform→draft rule versions→four-eyes publish.
    raise NotImplementedYet("Ingestion + four-eyes publishing is scaffolded (admin RBAC pending).")
