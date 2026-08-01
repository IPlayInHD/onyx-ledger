"""Financial data service — tax-year-scoped income & expense management."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.database.models import ExpenseCategory, ExpenseRecord, IncomeSource, IncomeType


class FinancialService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def add_income(
        self, user_id: uuid.UUID, tax_year: int, income_type_code: str,
        amount: Decimal, source_name: str | None = None,
    ) -> IncomeSource:
        itype = await self.s.scalar(select(IncomeType).where(IncomeType.code == income_type_code))
        if not itype:
            raise ValidationError(f"Unknown income type '{income_type_code}'")
        row = IncomeSource(
            user_id=user_id, tax_year=tax_year, income_type_id=itype.id,
            amount=amount, source_name=source_name,
        )
        self.s.add(row)
        await self.s.flush()
        return row

    async def list_income(self, user_id: uuid.UUID, tax_year: int) -> list[IncomeSource]:
        rows = await self.s.scalars(
            select(IncomeSource).where(
                IncomeSource.user_id == user_id,
                IncomeSource.tax_year == tax_year,
                IncomeSource.deleted_at.is_(None),
            )
        )
        return list(rows)

    async def add_expense(
        self, user_id: uuid.UUID, tax_year: int, category_code: str,
        amount: Decimal, description: str | None = None,
    ) -> ExpenseRecord:
        cat = await self.s.scalar(
            select(ExpenseCategory).where(ExpenseCategory.code == category_code)
        )
        if not cat:
            raise ValidationError(f"Unknown expense category '{category_code}'")
        row = ExpenseRecord(
            user_id=user_id, tax_year=tax_year, expense_category_id=cat.id,
            amount=amount, description=description,
        )
        self.s.add(row)
        await self.s.flush()
        return row

    async def list_expenses(self, user_id: uuid.UUID, tax_year: int) -> list[ExpenseRecord]:
        rows = await self.s.scalars(
            select(ExpenseRecord).where(
                ExpenseRecord.user_id == user_id,
                ExpenseRecord.tax_year == tax_year,
                ExpenseRecord.deleted_at.is_(None),
            )
        )
        return list(rows)
