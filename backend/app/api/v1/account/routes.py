"""Account lifecycle endpoints (Entry 11B1).

Deliberately two operations and a very small response. A deleting account
learns that deletion is under way and, once later phases have run, that it has
finished — never a phase name, a worker id, a table count or a queue position.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed_lifecycle_exempt
from app.services.privacy import AccountLifecycleService

router = APIRouter(prefix="/account", tags=["account"])


@router.post("/deletion", status_code=status.HTTP_202_ACCEPTED)
async def request_account_deletion(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed_lifecycle_exempt),
) -> dict:
    """Request deletion of the CALLER'S OWN account.

    The account is resolved from the authenticated token and nowhere else.
    There is no path parameter and no body: an endpoint that accepted an account
    id would be one authorization mistake away from letting anyone delete
    anyone, and no amount of checking afterwards is as good as not offering the
    parameter.

    202 rather than 200: the request is durable and access is already gone, but
    the purge it schedules has not run. Later 11B phases perform that, and until
    they exist this deliberately cannot report completion.

    Idempotent — a second request returns the same lifecycle rather than
    starting another.
    """
    lifecycle = await AccountLifecycleService(session).request_deletion(user_id)
    return lifecycle.as_response()


@router.get("/deletion")
async def get_account_deletion_status(
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed_lifecycle_exempt),
) -> dict:
    """The caller's own deletion status.

    Lifecycle-exempt for the obvious reason: a user who has asked to be deleted
    must still be able to see what happened to the request.
    """
    return (await AccountLifecycleService(session).status(user_id)).as_response()
