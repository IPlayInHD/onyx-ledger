"""Tax engine service — assembles engine inputs + a fact map from user data,
then runs the pure deterministic engine. No AI, no law hardcoded in Python
(the pure engine takes tax constants as data; opportunities come from the DB
rules evaluator).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ExpenseCategory,
    ExpenseRecord,
    IncomeSource,
    IncomeType,
    TaxProfile,
)
from app.services.tax_engine.core.engine import TaxInput, TaxResult, compute

ENGINE_VERSION = "py-1.0.0"

_INCOME_FIELD = {
    "employment": "employment_income",
    "self_employment": "self_employment_income",
    "business": "self_employment_income",
    "rental": "rental_income",
    "interest": "interest_income",
    "eligible_dividends": "eligible_dividends",
    "non_eligible_dividends": "non_eligible_dividends",
    "capital_gains": "capital_gains",
    "pension": "pension_income",
}
_EXPENSE_FIELD = {
    "medical": "medical_expenses",
    "tuition": "tuition",
    "childcare": "child_care",
    "donation": "donations",
}


class TaxEngineService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def build_input(self, user_id: uuid.UUID, tax_year: int) -> TaxInput:
        profile = await self.s.scalar(select(TaxProfile).where(TaxProfile.user_id == user_id))
        province = profile.province_code if profile else "ON"
        marital = (profile.marital_status if profile else None) or "single"
        inp = TaxInput(province=province or "ON", year=tax_year, marital_status=marital)

        itypes = {t.id: t.code for t in await self.s.scalars(select(IncomeType))}
        for row in await self.s.scalars(
            select(IncomeSource).where(
                IncomeSource.user_id == user_id, IncomeSource.tax_year == tax_year,
                IncomeSource.deleted_at.is_(None),
            )
        ):
            field = _INCOME_FIELD.get(itypes.get(row.income_type_id, ""), "other_income")
            setattr(inp, field, getattr(inp, field) + row.amount)

        cats = {c.id: c.code for c in await self.s.scalars(select(ExpenseCategory))}
        for row in await self.s.scalars(
            select(ExpenseRecord).where(
                ExpenseRecord.user_id == user_id, ExpenseRecord.tax_year == tax_year,
                ExpenseRecord.deleted_at.is_(None),
            )
        ):
            field = _EXPENSE_FIELD.get(cats.get(row.expense_category_id, ""))
            if field:
                setattr(inp, field, getattr(inp, field) + row.amount)
        return inp

    def run(self, inp: TaxInput) -> TaxResult:
        return compute(inp)

    def facts(self, inp: TaxInput, result: TaxResult) -> dict[str, object]:
        """Fact map keyed by fact_definition.fact_key for the rules evaluator."""
        return {
            "profile.province": inp.province,
            "profile.marital_status": inp.marital_status,
            "income.total": result.total_income,
            "income.net": result.net_income,
            "income.employment": inp.employment_income,
            "income.self_employment.net": inp.self_employment_income - inp.self_employment_expenses,
            "expense.medical.total": inp.medical_expenses,
            "expense.tuition.total": inp.tuition,
            "expense.donation.total": inp.donations,
            "derived.marginal_rate": result.marginal_rate,
            "derived.net_income": result.net_income,
            "rrsp.contribution": inp.rrsp_deduction,
        }
