from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.deps import AuthedSession, current_user_id
from app.core.exceptions import NotFound
from app.database.models import TaxProfile, UserAccount
from app.schemas import TaxProfileIn, TaxProfileOut
from app.services.users.profile_service import ProfileService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def get_me(
    session: AuthedSession,
    user_id: uuid.UUID = Depends(current_user_id),
) -> dict:
    user = await session.get(UserAccount, user_id)
    if user is None:
        raise NotFound("User not found")
    return {"id": str(user.id), "email": user.email, "status": user.status}


@router.get("/me/tax-profile", response_model=TaxProfileOut)
async def get_tax_profile(
    session: AuthedSession,
    user_id: uuid.UUID = Depends(current_user_id),
) -> TaxProfileOut:
    prof = await session.get(TaxProfile, user_id)
    if prof is None:
        raise NotFound("Tax profile not set")
    return TaxProfileOut.model_validate(prof)


@router.put("/me/tax-profile", response_model=TaxProfileOut)
async def upsert_tax_profile(
    body: TaxProfileIn,
    session: AuthedSession,
    user_id: uuid.UUID = Depends(current_user_id),
) -> TaxProfileOut:
    # The route delegates: the mutation and its freshness event belong in the
    # service, so an import or an administrative write cannot skip invalidation
    # by not going through HTTP.
    prof, _ = await ProfileService(session).upsert_tax_profile(
        user_id, body.model_dump())
    return TaxProfileOut.model_validate(prof)
