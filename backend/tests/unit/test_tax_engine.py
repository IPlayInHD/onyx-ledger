"""Golden-case + property tests for the pure tax engine.

Golden values are the hand-derived reference figures validated in the reference
prototype (CRA published method, 2025 blended 14.5%).
"""
from decimal import Decimal

import pytest

from app.services.tax_engine.core.engine import TaxInput, compute

D = lambda x: Decimal(str(x))  # noqa: E731


def approx(a: Decimal, b: float, tol: float = 1.5) -> bool:
    return abs(float(a) - b) <= tol


def test_ontario_60k_2025_golden():
    r = compute(TaxInput(
        province="ON", year=2025, employment_income=D(60000),
        cpp_contrib=D("3361.75"), ei_contrib=D(984), tax_withheld=D(0),
    ))
    assert approx(r.federal_tax, 5675.37)
    assert approx(r.provincial_tax, 3058.49)     # incl. $600 health premium
    assert approx(r.income_tax, 8733.86)


def test_self_employed_60k_has_schedule8_cpp():
    r = compute(TaxInput(province="ON", year=2025, self_employment_income=D(60000)))
    # self-employed CPP (both halves) on the net $60k
    assert approx(r.cpp_payable_se, 6723.50)
    # Deduction per Schedule 8 (5000-S8 E (25)) Part 4 line 17: half the base
    # contribution (5,593.50 / 2) plus the full first additional (1,130.00) =
    # $3,926.75; the credit takes only the other half of the base.
    # $23.17 below the pre-fix figure: the extra $565 deduction lands at
    # Ontario's 9.15% bracket while the credit it replaced was worth 5.05%.
    assert approx(r.net_income, 56073.25)
    assert approx(r.total_payable, 14887.30, tol=2.0)


def test_rental_is_ordinary_income():
    r = compute(TaxInput(
        province="ON", year=2025, employment_income=D(70000),
        cpp_contrib=D("3867.50"), ei_contrib=D("1077.48"),
        rental_income=D(24000), rental_expenses=D(9000),
    ))
    assert approx(r.total_income, 85000)          # 70k employment + 15k net rental


@pytest.mark.parametrize("prov", ["ON", "BC", "AB", "QC"])
def test_tax_is_non_negative_and_reconciles(prov):
    r = compute(TaxInput(province=prov, year=2025, employment_income=D(85000),
                         cpp_contrib=D("4034.10"), ei_contrib=D("1077.48")))
    assert r.federal_tax >= 0 and r.provincial_tax >= 0
    # federal + provincial reconcile to the reported income tax
    assert approx(r.federal_tax + r.provincial_tax, float(r.income_tax), tol=0.05)


def test_monotonic_in_income():
    lo = compute(TaxInput(province="ON", year=2025, employment_income=D(50000)))
    hi = compute(TaxInput(province="ON", year=2025, employment_income=D(90000)))
    assert hi.income_tax > lo.income_tax
    assert hi.marginal_rate >= hi.average_rate     # marginal ≥ average


def test_determinism():
    args = dict(province="BC", year=2025, employment_income=D(72000),
                cpp_contrib=D("4034.10"), ei_contrib=D("1077.48"))
    assert compute(TaxInput(**args)) == compute(TaxInput(**args))
