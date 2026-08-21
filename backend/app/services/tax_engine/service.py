"""Tax engine service — assembles engine inputs + a fact map from user data,
then runs the pure deterministic engine. No AI, no law hardcoded in Python
(the pure engine takes tax constants as data; opportunities come from the DB
rules evaluator).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Asset,
    ExpenseCategory,
    ExpenseRecord,
    IncomeSource,
    IncomeType,
    RegisteredAccountDetail,
    TaxProfile,
)
from app.services.tax_engine.core.data import TaxDataset
from app.services.tax_engine.core.engine import TaxInput, TaxResult, compute
from app.services.tax_engine.core.provider import TaxDataProvider

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

    @staticmethod
    def build_input_from_snapshot(snapshot: dict) -> TaxInput:
        """Reconstruct the engine input from a FROZEN snapshot payload.

        The only input builder the optimization compute path may call. It takes
        a payload, not a user id, so there is no parameter through which live
        state could be reached.
        """
        from app.services.ioe.frozen.models import reconstruct_tax_input

        return reconstruct_tax_input(snapshot)

    async def build_input_from_live_sources(
        self, user_id: uuid.UUID, tax_year: int
    ) -> TaxInput:
        """Read the user's CURRENT financial state.

        Legitimate when an analysis is being created — that is the moment the
        snapshot is taken. Forbidden after TX-1 of an optimization: calling it
        there is what let a run be sealed under one snapshot's identity while
        being calculated from different numbers.
        """
        return await self._build_input_live(user_id, tax_year)

    async def _build_input_live(self, user_id: uuid.UUID, tax_year: int) -> TaxInput:
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
            income_field = _INCOME_FIELD.get(
                itypes.get(row.income_type_id, ""), "other_income")
            setattr(inp, income_field, getattr(inp, income_field) + row.amount)

        cats = {c.id: c.code for c in await self.s.scalars(select(ExpenseCategory))}
        # A distinct loop variable: reusing `row` made the expense rows look
        # like income rows to a reader and to the type checker, which is how
        # `expense_category_id` came to be read off an `IncomeSource`.
        for expense in await self.s.scalars(
            select(ExpenseRecord).where(
                ExpenseRecord.user_id == user_id, ExpenseRecord.tax_year == tax_year,
                ExpenseRecord.deleted_at.is_(None),
            )
        ):
            expense_field = _EXPENSE_FIELD.get(
                cats.get(expense.expense_category_id, ""))
            if expense_field:
                setattr(inp, expense_field,
                        getattr(inp, expense_field) + expense.amount)

        # ACTUAL registered-account contributions — recorded facts, not
        # hypothetical levers. `wealth.registered_account_detail` is the typed
        # representation of contributions already made this tax year; a
        # scenario lever models one that has NOT been made. Only the two
        # deduction-bearing account types the engine computes today are read.
        for detail in await self.s.scalars(
            select(RegisteredAccountDetail)
            .join(Asset, Asset.id == RegisteredAccountDetail.asset_id)
            .where(
                Asset.user_id == user_id, Asset.deleted_at.is_(None),
                RegisteredAccountDetail.tax_year == tax_year,
                RegisteredAccountDetail.registered_type.in_(("RRSP", "FHSA")),
            )
        ):
            field = ("rrsp_deduction" if detail.registered_type == "RRSP"
                     else "fhsa_deduction")
            setattr(inp, field, getattr(inp, field) + detail.contributions_ytd)
        return inp

    async def resolve_dataset(self, tax_year: int) -> TaxDataset:
        """Resolve the governed reference data for a tax year.

        The one place a calculation's constants are fetched. Callers resolve
        ONCE per run and pass the result into every `run`/`compute` that run
        performs, so a baseline and the candidates measured against it cannot
        come from different tax law.
        """
        return await TaxDataProvider(self.s).resolve(tax_year)

    def run(self, inp: TaxInput, dataset: TaxDataset | None = None) -> TaxResult:
        """Run the engine. Stays synchronous, and still never fetches.

        Passing `dataset` is what makes the calculation read governed reference
        data; omitting it uses the in-code bootstrap, which is what every caller
        got before the provider existed.
        """
        return compute(inp, dataset)

    def facts(self, inp: TaxInput, result: TaxResult) -> dict[str, object]:
        """Fact map keyed by fact_definition.fact_key for the rules evaluator."""
        return self.facts_for(inp, result)

    @staticmethod
    def facts_for(inp: TaxInput, result: TaxResult) -> dict[str, object]:
        """The same mapping, without a session.

        Portfolio assembly re-derives facts from HYPOTHETICAL inputs and must
        get a map identical to the one the initial evaluation used; sharing this
        one function is what guarantees that rather than hoping for it.
        """
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
