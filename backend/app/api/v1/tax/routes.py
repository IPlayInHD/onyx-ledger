from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.database.models import TaxRule, TaxRuleVersion

router = APIRouter(prefix="/tax", tags=["tax"])


@router.get("/rules")
async def list_published_rules(
    tax_year: int = Query(..., ge=1900, le=2200),
    _user: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[dict]:
    """Published tax rules in force for a year (the versioned KB, read-only)."""
    result = await session.execute(
        select(TaxRule.code, TaxRule.name, TaxRule.category, TaxRuleVersion.tax_year,
               TaxRuleVersion.description, TaxRuleVersion.source_url)
        .join(TaxRuleVersion, TaxRuleVersion.tax_rule_id == TaxRule.id)
        .where(TaxRuleVersion.tax_year == tax_year, TaxRuleVersion.status == "published")
    )
    return [
        {"code": r.code, "name": r.name, "category": r.category, "tax_year": r.tax_year,
         "description": r.description, "source_url": r.source_url}
        for r in result
    ]
