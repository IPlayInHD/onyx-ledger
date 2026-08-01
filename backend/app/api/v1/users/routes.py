from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.core.exceptions import NotFound
from app.database.models import TaxProfile, UserAccount
from app.schemas import TaxProfileIn, TaxProfileOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def get_me(
    user_id: uuid.UUID = Depends(current_user_id), session: AsyncSession = Depends(db_authed)
) -> dict:
    user = await session.get(UserAccount, user_id)
    if user is None:
        raise NotFound("User not found")
    return {"id": str(user.id), "email": user.email, "status": user.status}


@router.get("/me/tax-profile", response_model=TaxProfileOut)
async def get_tax_profile(
    user_id: uuid.UUID = Depends(current_user_id), session: AsyncSession = Depends(db_authed)
) -> TaxProfileOut:
    prof = await session.get(TaxProfile, user_id)
    if prof is None:
        raise NotFound("Tax profile not set")
    return TaxProfileOut.model_validate(prof)


@router.put("/me/tax-profile", response_model=TaxProfileOut)
async def upsert_tax_profile(
    body: TaxProfileIn,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> TaxProfileOut:
    prof = await session.get(TaxProfile, user_id)
    if prof is None:
        prof = TaxProfile(user_id=user_id)
        session.add(prof)
    for field, value in body.model_dump().items():
        setattr(prof, field, value)
    await session.flush()
    return TaxProfileOut.model_validate(prof)
