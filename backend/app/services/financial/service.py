"""Financial data service — tax-year-scoped income & expense management."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.database.models import ExpenseCategory, ExpenseRecord, IncomeSource, IncomeType
from app.services.ioe.freshness_producers import on_financial_data_changed


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
        # Emitted INSIDE the caller's transaction, so the event and the income
        # row share one fate: a rollback leaves neither. The row id is the
        # change token, so a retry of this same insert collides on the dedupe
        # key while a genuinely new row invalidates again.
        await on_financial_data_changed(
            self.s, user_id, tax_year, change_token=f"income:{row.id}")
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
        await on_financial_data_changed(
            self.s, user_id, tax_year, change_token=f"expense:{row.id}")
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

    async def delete_income_source(
        self, user_id: uuid.UUID, income_id: uuid.UUID, tax_year: int,
    ) -> bool:
        """Remove one owned income source. Returns False if it was already gone.

        HARD DELETE, not a tombstone. The row's CONTENT is the personal data —
        the amount, `source_name`, `notes` — so a tombstone would retain
        precisely what deletion is for. Nothing sealed needs the live row: no
        foreign key from `analysis.*` or `ioe.*` points at any source table,
        which is why replay reads the frozen snapshot instead.

        Deleting it cascades `docs.document_link`, and that is correct rather
        than incidental. This row IS the confirmed fact; the link is provenance
        FOR it. Entry 11B4 retained that edge when the DOCUMENT was deleted,
        because the fact outlived its source document — here the fact itself is
        going, and an edge whose subject no longer exists claims evidence for
        nothing.

        The tax year is required and is not a convenience: `finance.income_source`
        is partitioned by it, and the primary key is (id, tax_year). Naming it
        lets PostgreSQL prune to one partition rather than scan them all.
        """
        row = await self.s.get(IncomeSource, (income_id, tax_year))
        # NotFound-as-False for someone else's row as well as a missing one: a
        # distinguishable "forbidden" would confirm the id exists.
        if row is None or row.user_id != user_id or row.deleted_at is not None:
            return False

        await self.s.delete(row)
        await self.s.flush()
        # Same transaction as the delete, so the event and the removal share one
        # fate. The row id is the change token: deleting the same row twice
        # cannot emit twice, because the second call finds nothing.
        await on_financial_data_changed(
            self.s, user_id, tax_year, change_token=f"income-deleted:{income_id}")
        return True

    async def delete_expense(
        self, user_id: uuid.UUID, expense_id: uuid.UUID, tax_year: int,
    ) -> bool:
        """Remove one owned expense. See `delete_income_source` — same contract,
        same reasoning, same partitioned primary key."""
        row = await self.s.get(ExpenseRecord, (expense_id, tax_year))
        if row is None or row.user_id != user_id or row.deleted_at is not None:
            return False

        await self.s.delete(row)
        await self.s.flush()
        await on_financial_data_changed(
            self.s, user_id, tax_year, change_token=f"expense-deleted:{expense_id}")
        return True
