from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.schemas import ExpenseIn, IncomeIn, IncomeOut
from app.services.admission import OperationClass, admission_guard
from app.services.admission.guard import user_scope
from app.services.financial.service import FinancialService

router = APIRouter(prefix="/financials", tags=["financials"])


@router.post("/income", response_model=IncomeOut, status_code=status.HTTP_201_CREATED)
async def add_income(
    body: IncomeIn,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> IncomeOut:
    """Record an income source.

    NORMAL_WRITE: cheap per call, and an UNBOUNDED row creator. RLS keeps the
    rows inside one tenant, which bounds who can read them and not how many
    there are — a script here fills a tenant's own financial tables and, through
    them, the cost of every later analysis over that year.
    """
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        row = await FinancialService(session).add_income(
            user_id, body.tax_year, body.income_type_code, body.amount, body.source_name
        )
        return IncomeOut.model_validate(row)


@router.get("/income", response_model=list[IncomeOut])
async def list_income(
    tax_year: int = Query(..., ge=1900, le=2200),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[IncomeOut]:
    rows = await FinancialService(session).list_income(user_id, tax_year)
    return [IncomeOut.model_validate(r) for r in rows]


@router.post("/expenses", status_code=status.HTTP_201_CREATED)
async def add_expense(
    body: ExpenseIn,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        row = await FinancialService(session).add_expense(
            user_id, body.tax_year, body.expense_category_code, body.amount, body.description
        )
        return {"id": str(row.id), "tax_year": row.tax_year, "amount": str(row.amount)}
