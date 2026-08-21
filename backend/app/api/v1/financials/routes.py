from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user_id, db_authed
from app.schemas import (
    ExpenseIn,
    IncomeIn,
    IncomeOut,
    RegisteredAccountIn,
    RegisteredAccountOut,
)
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


@router.post("/registered-accounts", response_model=RegisteredAccountOut,
             status_code=status.HTTP_201_CREATED)
async def add_registered_account(
    body: RegisteredAccountIn,
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> RegisteredAccountOut:
    """Record an actual RRSP/FHSA contribution for a tax year — a fact the
    baseline analysis reads, as opposed to a scenario lever, which models a
    contribution not yet made."""
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        row = await FinancialService(session).add_registered_account(
            user_id, body.tax_year, body.registered_type,
            body.contributions_ytd, body.contribution_room, body.label,
        )
        return RegisteredAccountOut.model_validate(row)


@router.get("/registered-accounts", response_model=list[RegisteredAccountOut])
async def list_registered_accounts(
    tax_year: int = Query(..., ge=1900, le=2200),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> list[RegisteredAccountOut]:
    rows = await FinancialService(session).list_registered_accounts(user_id, tax_year)
    return [RegisteredAccountOut.model_validate(r) for r in rows]


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


@router.delete("/income/{income_id}", status_code=status.HTTP_200_OK)
async def delete_income(
    income_id: uuid.UUID,
    tax_year: int = Query(..., ge=1900, le=2200),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """Delete one owned income source.

    The tax year is a required query parameter rather than something the server
    looks up, because the table is partitioned on it and the primary key is
    (id, tax_year). Supplying it prunes to one partition; without it the server
    would have to scan every year to find a row the caller already knows the
    year of.

    It is NOT authorization — ownership is checked against the row, and a wrong
    year simply finds nothing.

    NORMAL_WRITE admission: cheap per call, but it is a mutation and an
    unbounded caller should not be able to drive deletions in a loop for free.
    """
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        deleted = await FinancialService(session).delete_income_source(
            user_id, income_id, tax_year)
        # Minimal by design: no row count, no partition, no freshness event id.
        # "deleted" is true whether this call did the work or found it already
        # done — the caller asked for a state, not for a history.
        return {"status": "deleted", "already_deleted": not deleted}


@router.delete("/expenses/{expense_id}", status_code=status.HTTP_200_OK)
async def delete_expense(
    expense_id: uuid.UUID,
    tax_year: int = Query(..., ge=1900, le=2200),
    user_id: uuid.UUID = Depends(current_user_id),
    session: AsyncSession = Depends(db_authed),
) -> dict:
    """Delete one owned expense. Same contract as `delete_income`."""
    async with admission_guard(
        OperationClass.NORMAL_WRITE, scope_id=user_scope(user_id)
    ):
        deleted = await FinancialService(session).delete_expense(
            user_id, expense_id, tax_year)
        return {"status": "deleted", "already_deleted": not deleted}
