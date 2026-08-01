"""Pure, deterministic Canadian personal-tax engine.

No I/O, no framework, no AI. Ported from the validated reference engine: federal
+ provincial brackets, BPA phase-down, CPP/EI credits, the Canada employment
amount, dividend gross-up/DTC, 50% capital-gains inclusion, Ontario surtax +
health premium, the Quebec abatement, self-employed CPP (Schedule 8), and net
rental income. Same inputs always yield the same result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.services.tax_engine.core.data import (
    FEDERAL_2025,
    PROVINCES_2025,
    Bracket,
    FederalData,
    ProvincialData,
)

Q = Decimal("0.01")


def _r(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Q, rounding=ROUND_HALF_UP)


def _pos(x: Decimal) -> Decimal:
    return x if x > 0 else Decimal(0)


@dataclass
class TaxInput:
    province: str = "ON"
    year: int = 2025
    age: int | None = None
    marital_status: str = "single"
    spouse_net_income: Decimal = Decimal(0)
    employment_income: Decimal = Decimal(0)
    self_employment_income: Decimal = Decimal(0)
    self_employment_expenses: Decimal = Decimal(0)
    rental_income: Decimal = Decimal(0)
    rental_expenses: Decimal = Decimal(0)
    interest_income: Decimal = Decimal(0)
    eligible_dividends: Decimal = Decimal(0)
    non_eligible_dividends: Decimal = Decimal(0)
    capital_gains: Decimal = Decimal(0)
    pension_income: Decimal = Decimal(0)
    other_income: Decimal = Decimal(0)
    rrsp_deduction: Decimal = Decimal(0)
    fhsa_deduction: Decimal = Decimal(0)
    union_dues: Decimal = Decimal(0)
    child_care: Decimal = Decimal(0)
    other_deductions: Decimal = Decimal(0)
    tuition: Decimal = Decimal(0)
    medical_expenses: Decimal = Decimal(0)
    donations: Decimal = Decimal(0)
    cpp_contrib: Decimal = Decimal(0)
    ei_contrib: Decimal = Decimal(0)
    tax_withheld: Decimal = Decimal(0)


@dataclass
class TaxResult:
    year: int
    province: str
    total_income: Decimal
    total_deductions: Decimal
    net_income: Decimal
    taxable_income: Decimal
    federal_tax: Decimal
    provincial_tax: Decimal
    surtax: Decimal
    health_premium: Decimal
    income_tax: Decimal
    cpp_payable_se: Decimal
    total_payable: Decimal
    tax_withheld: Decimal
    refund_or_balance: Decimal
    is_refund: bool
    marginal_rate: Decimal
    average_rate: Decimal
    line_items: list[dict] = field(default_factory=list)


def _bracket_tax(income: Decimal, brackets: list[Bracket]) -> Decimal:
    tax = Decimal(0)
    lower = Decimal(0)
    for b in brackets:
        upper = b.up_to if b.up_to is not None else income
        if income > lower:
            tax += (min(income, upper) - lower) * b.rate
            lower = upper if b.up_to is not None else income
        else:
            break
    return tax


def _federal_bpa(net_income: Decimal, f: FederalData) -> Decimal:
    if net_income <= f.bpa_phase_start:
        return f.bpa_max
    if net_income >= f.bpa_phase_end:
        return f.bpa_min
    frac = (net_income - f.bpa_phase_start) / (f.bpa_phase_end - f.bpa_phase_start)
    return f.bpa_max - (f.bpa_max - f.bpa_min) * frac


def _surtax(prov_tax: Decimal, tiers: list[tuple[Decimal, Decimal]]) -> Decimal:
    s = Decimal(0)
    for over, rate in tiers:
        if prov_tax > over:
            s += (prov_tax - over) * rate
    return s


def _health_premium(taxable: Decimal, table: list[tuple[Decimal, Decimal]]) -> Decimal:
    for up_to, amount in table:
        if up_to is None or taxable <= up_to:
            return amount
    return Decimal(0)


def _marginal_total(ti: Decimal, f: FederalData, p: ProvincialData) -> Decimal:
    fed = _bracket_tax(ti, f.brackets) * (Decimal(1) - p.abatement)
    prov = _bracket_tax(ti, p.brackets)
    prov += _surtax(_pos(prov - p.credit_rate * p.bpa), p.surtax)
    return fed + prov


def compute(inp: TaxInput) -> TaxResult:
    f = FEDERAL_2025
    p = PROVINCES_2025.get(inp.province, PROVINCES_2025["ON"])

    # ---- Income ----
    taxable_cap_gains = inp.capital_gains * f.capital_gains_inclusion
    grossed_elig = inp.eligible_dividends * (Decimal(1) + f.eligible_div_gross_up)
    grossed_nonelig = inp.non_eligible_dividends * (Decimal(1) + f.non_eligible_div_gross_up)
    net_se = inp.self_employment_income - inp.self_employment_expenses
    net_rental = inp.rental_income - inp.rental_expenses

    # ---- Self-employed CPP (Schedule 8): both halves on incremental base ----
    cpp_ceiling = f.cpp_max + f.cpp2_max
    emp_base = _pos(min(inp.employment_income, f.cpp_max_pensionable) - f.cpp_exemption)
    tot_base = _pos(min(inp.employment_income + _pos(net_se), f.cpp_max_pensionable) - f.cpp_exemption)
    se_cpp_base = _pos(tot_base - emp_base)
    se_cpp_total = _r(se_cpp_base * f.cpp_rate * 2)
    se_cpp_deduction = _r(se_cpp_total / 2)
    se_cpp_credit = se_cpp_total / 2

    total_income = (
        inp.employment_income + net_se + inp.interest_income + grossed_elig + grossed_nonelig
        + taxable_cap_gains + inp.pension_income + inp.other_income + net_rental
    )
    total_deductions = (
        inp.rrsp_deduction + inp.fhsa_deduction + inp.union_dues + inp.child_care
        + inp.other_deductions + se_cpp_deduction
    )
    net_income = _pos(total_income - total_deductions)
    taxable_income = net_income

    # ---- Shared credit amounts ----
    cpp = min(inp.cpp_contrib + se_cpp_credit, cpp_ceiling)
    ei = min(inp.ei_contrib, f.ei_max)
    canada_employment = (
        min(f.canada_employment, inp.employment_income) if inp.employment_income > 0 else Decimal(0)
    )
    partnered = inp.marital_status in ("married", "common_law")
    spousal = _pos(f.bpa_max - inp.spouse_net_income) if partnered else Decimal(0)
    medical_threshold = min(net_income * f.medical_pct, f.medical_cap)
    medical_eligible = _pos(inp.medical_expenses - medical_threshold)

    # ---- Federal tax ----
    fed_before = _bracket_tax(taxable_income, f.brackets)
    fed_bpa = _federal_bpa(net_income, f)
    fed_credit_base = (
        fed_bpa + cpp + ei + canada_employment + inp.tuition + spousal + medical_eligible
    )
    fed_nonref = f.credit_rate * fed_credit_base + _donation_credit(inp.donations, f)
    fed_dtc = grossed_elig * f.eligible_div_dtc + grossed_nonelig * f.non_eligible_div_dtc
    federal_tax = _pos(fed_before - fed_nonref - fed_dtc)
    federal_tax *= (Decimal(1) - p.abatement)

    # ---- Provincial tax ----
    prov_before = _bracket_tax(taxable_income, p.brackets)
    prov_credit_base = p.bpa + cpp + ei + inp.tuition + (min(spousal, p.bpa) if spousal > 0 else Decimal(0)) + medical_eligible
    prov_nonref = p.credit_rate * prov_credit_base
    provincial_tax = _pos(prov_before - prov_nonref)
    surtax = _surtax(provincial_tax, p.surtax)
    provincial_tax += surtax
    ohp = _health_premium(taxable_income, p.health_premium)
    provincial_tax += ohp

    income_tax = _r(federal_tax + provincial_tax)
    cpp_payable_se = se_cpp_total
    total_payable = _r(income_tax + cpp_payable_se)
    refund_or_balance = _r(inp.tax_withheld - total_payable)

    marginal = _r((_marginal_total(taxable_income + 1000, f, p) - _marginal_total(taxable_income, f, p)) / 1000)
    average = _r(income_tax / total_income) if total_income > 0 else Decimal(0)

    line_items = [
        {"kind": "income", "label": "Total income", "amount": _r(total_income)},
        {"kind": "deduction", "label": "Total deductions", "amount": _r(total_deductions)},
        {"kind": "income", "label": "Taxable income", "amount": _r(taxable_income)},
        {"kind": "tax", "label": "Federal tax", "amount": _r(federal_tax)},
        {"kind": "tax", "label": "Provincial tax", "amount": _r(provincial_tax)},
        {"kind": "tax", "label": "Total income tax", "amount": income_tax},
    ]
    if cpp_payable_se > 0:
        line_items.append(
            {"kind": "payable", "label": "CPP payable (self-employment)", "amount": cpp_payable_se}
        )

    return TaxResult(
        year=inp.year, province=inp.province,
        total_income=_r(total_income), total_deductions=_r(total_deductions),
        net_income=_r(net_income), taxable_income=_r(taxable_income),
        federal_tax=_r(federal_tax), provincial_tax=_r(provincial_tax),
        surtax=_r(surtax), health_premium=_r(ohp), income_tax=income_tax,
        cpp_payable_se=_r(cpp_payable_se), total_payable=total_payable,
        tax_withheld=_r(inp.tax_withheld), refund_or_balance=refund_or_balance,
        is_refund=refund_or_balance >= 0, marginal_rate=marginal, average_rate=average,
        line_items=line_items,
    )


def _donation_credit(donations: Decimal, f: FederalData) -> Decimal:
    if donations <= 0:
        return Decimal(0)
    first = min(donations, Decimal(200)) * f.donation_low
    rest = _pos(donations - Decimal(200)) * f.donation_high
    return first + rest
