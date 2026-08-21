"""Launch-blocker remediation goldens: H1 Ontario donation credit and
H2 fail-closed jurisdictions.

Expected values are independent arithmetic transcribed from the registered
ON428 capture (5006-C 2025, RAW_BYTES_SHA256 be18f03a…): line 47 = Schedule 9
line 13 x 5.05%, line 48 = Schedule 9 line 14 x 11.16% — never read back from
the engine under test.
"""
from decimal import Decimal

import pytest

from app.services.tax_engine.core.data import bootstrap_dataset
from app.services.tax_engine.core.engine import (
    TaxInput,
    UnsupportedJurisdiction,
    compute,
)

D = lambda x: Decimal(str(x))  # noqa: E731

ALL_CODES = ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU",
             "ON", "PE", "QC", "SK", "YT"]
SUPPORTED = {"AB", "BC", "ON", "QC"}


def on428_donation_credit(donations: Decimal) -> Decimal:
    """ON428 lines 47-49, transcribed from the registered form."""
    if donations <= 0:
        return Decimal(0)
    first = min(donations, D(200)) * D("0.0505")
    over = max(donations - D(200), Decimal(0))
    return first + over * D("0.1116")


def federal_118_1_credit(donations: Decimal, taxable: Decimal) -> Decimal:
    """ITA 118.1(3) oracle (as certified in the tax-correctness entry)."""
    if donations <= 0:
        return Decimal(0)
    first = min(donations, D(200)) * D("0.145")
    over = max(donations - D(200), Decimal(0))
    top = min(over, max(taxable - D(253414), Decimal(0)))
    return first + top * D("0.33") + (over - top) * D("0.29")


def deltas(income: Decimal, donations: Decimal, year: int = 2025):
    base = compute(TaxInput(province="ON", year=year, employment_income=income))
    with_don = compute(TaxInput(province="ON", year=year,
                                employment_income=income, donations=donations))
    return (base.federal_tax - with_don.federal_tax,
            base.provincial_tax - with_don.provincial_tax)


@pytest.mark.parametrize("donations", [D(0), D(150), D(200), D(201), D(10000)])
def test_ontario_donation_credit_matches_the_form_below_surtax(donations):
    # 90k keeps net Ontario tax below the surtax threshold in both runs, so
    # the provincial delta IS the ON428 line-49 credit, exactly once. The two
    # totals are each rounded to the cent independently, so their difference
    # may sit within one cent of the exact credit — never more.
    fed_delta, prov_delta = deltas(D(90000), donations)
    assert abs(prov_delta - on428_donation_credit(donations)) <= D("0.01")
    assert abs(fed_delta - federal_118_1_credit(donations, D(90000))) <= D("0.01")


def test_ontario_credit_is_applied_before_surtax():
    # High income: Ontario surtax (20% + 36%) applies, and ON428 computes it
    # AFTER line 50 credits — so the provincial saving exceeds the bare credit
    # by exactly the surtax on the credited amount (1.56x), to the cent.
    credit = on428_donation_credit(D(10000))
    _, prov_delta = deltas(D(310000), D(10000))
    assert abs(prov_delta - credit * D("1.56")) <= D("0.01")
    assert prov_delta > credit  # the ordering itself: credit precedes surtax


def test_high_income_federal_33_tier_is_unchanged():
    fed_delta, _ = deltas(D(310000), D(50000))
    assert fed_delta == federal_118_1_credit(D(50000), D(310000))


def test_2026_dataset_carries_the_ontario_rates():
    fed_delta, prov_delta = deltas(D(90000), D(1200), year=2026)
    assert prov_delta == on428_donation_credit(D(1200))
    assert fed_delta > 0


def test_other_bootstrap_provinces_get_no_invented_donation_credit():
    # BC/AB/QC donation authority is not registered; their rates stay zero and
    # their behaviour stays exactly what it was before this entry.
    for prov in ("BC", "AB", "QC"):
        base = compute(TaxInput(province=prov, year=2025, employment_income=D(90000)))
        with_don = compute(TaxInput(province=prov, year=2025,
                                    employment_income=D(90000), donations=D(10000)))
        assert base.provincial_tax == with_don.provincial_tax


@pytest.mark.parametrize("code", ALL_CODES)
def test_all_thirteen_codes_fail_closed_or_compute(code):
    inp = TaxInput(province=code, year=2025, employment_income=D(60000))
    if code in SUPPORTED:
        assert compute(inp).income_tax > 0
    else:
        with pytest.raises(UnsupportedJurisdiction):
            compute(inp)


def test_unsupported_jurisdiction_error_names_the_supported_set():
    with pytest.raises(UnsupportedJurisdiction) as exc:
        compute(TaxInput(province="YT", year=2025, employment_income=D(1)))
    msg = str(exc.value)
    assert "YT" in msg and "ON" in msg


def test_supported_set_reads_from_the_dataset_not_a_hardcoded_list():
    ds = bootstrap_dataset(2025)
    assert set(ds.provinces) == SUPPORTED
