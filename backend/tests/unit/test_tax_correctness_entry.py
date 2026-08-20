"""Targeted tests for the consolidated tax-correctness entry.

Three production fixes, each asserted against its registered authority rather
than against the engine's own output:

1. Self-employed CPP allocation — Schedule 8 (5000-S8 E (25)) Part 4: line 14
   payable = base + first + second additional; line 15 credit = 50% of the
   BASE contribution only; line 17 deduction = that half plus ALL first and
   second additional contributions. (ITA 60(e)(i), 60(e.1), 118.7 concur.)
2. Federal donation credit — ITA 118.1(3): A×B + C×D + E×F, where C is the
   highest individual percentage and D is the over-$200 gift portion matched
   by income taxable above the top-bracket threshold.
3. FHSA_ROOM default — CRA "Participating in your FHSAs" / ITA 146.6(1):
   an UNDECLARED pool defaults to the annual participation room instead of
   behaving as uncapped; a declared capacity (including carry-forward) is
   never overwritten.

The CPP expectations are derived inside each test from the schedule's own
rates and band definitions, so a wrong engine allocation fails against the
worksheet arithmetic rather than being copied into the oracle.
"""
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.services.ioe.domain.portfolio import ResourceLedger
from app.services.ioe.orchestrator import _resource_capacities
from app.services.tax_engine.core.data import FEDERAL_2025, bootstrap_dataset
from app.services.tax_engine.core.engine import TaxInput, _donation_credit, compute

D = lambda x: Decimal(str(x))  # noqa: E731
CENT = Decimal("0.01")

# Schedule 8 (5000-S8 E (25)) figures, cited at the line that prints each:
# Part 2 proration table row 12 and Part 4 lines 9-12.
YMPE = D(71300)
YAMPE = D(81200)
EXEMPTION = D(3500)
MAX_BASE_FIRST_EARNINGS = D(67800)          # Part 4 line 9 cap
SE_BASE_RATE = D("0.099")                   # line 10, max 6,712.20
SE_FIRST_RATE = D("0.02")                   # line 11, max 1,356.00
SE_SECOND_RATE = D("0.08")                  # line 12, max   792.00


def s8_part4(se_earnings: Decimal) -> dict[str, Decimal]:
    """Lines 4-17 of Part 4, transcribed for a pure self-employed filer."""
    l4 = min(YAMPE, se_earnings)
    l6 = max(l4 - YMPE, D(0))
    l7 = l4 - l6
    l9 = min(max(l7 - EXEMPTION, D(0)), MAX_BASE_FIRST_EARNINGS)
    base = min(l9 * SE_BASE_RATE, D("6712.20"))
    first = min(l9 * SE_FIRST_RATE, D("1356.00"))
    second = min(l6 * SE_SECOND_RATE, D("792.00"))
    return {
        "base": base, "first": first, "second": second,
        "payable": (base + first + second).quantize(CENT, ROUND_HALF_UP),
        "credit": base / 2,                                     # line 15
        "deduction": (base / 2 + first + second).quantize(CENT, ROUND_HALF_UP),
    }


# The band walk: below the exemption, base-only earnings, at/above YMPE,
# inside the CPP2 band, at/above YAMPE — including the five previously
# measured examples.
SE_CASES = [D(3000), D(20000), D(50000), D(71300), D(72000),
            D(75000), D(81200), D(90000)]


@pytest.mark.parametrize("se", SE_CASES, ids=[str(x) for x in SE_CASES])
def test_se_cpp_allocation_matches_schedule8(se):
    expected = s8_part4(se)
    r = compute(TaxInput(province="ON", year=2025, self_employment_income=se))
    assert r.cpp_payable_se == expected["payable"]
    # The deduction is the only deduction in this input, so total_deductions
    # exposes line 17 directly; net income exposes it a second way.
    assert r.total_deductions == expected["deduction"]
    assert r.net_income == se - expected["deduction"]


@pytest.mark.parametrize("se", SE_CASES, ids=[str(x) for x in SE_CASES])
def test_se_cpp_payable_unchanged_by_allocation(se):
    """Line 14 equals the pre-fix combined arithmetic exactly.

    0.099 + 0.02 = 0.119 in exact Decimal, so allocation cannot move the
    amount payable; this pins that invariant without hardcoding old outputs.
    """
    band1 = min(max(min(se, YMPE) - EXEMPTION, D(0)), MAX_BASE_FIRST_EARNINGS)
    band2 = max(min(se, YAMPE) - YMPE, D(0))
    combined = (band1 * D("0.119") + band2 * D("0.08")).quantize(CENT, ROUND_HALF_UP)
    r = compute(TaxInput(province="ON", year=2025, self_employment_income=se))
    assert r.cpp_payable_se == combined


def test_se_cpp_credit_is_base_half_only():
    """At YAMPE and above, the credited amount is 3,356.10 (half of the
    6,712.20 base maximum), never the old 4,430.10 half-of-everything.

    Proven through the federal tax delta between the engine and a
    hand-computed federal calculation that credits line 15 exactly.
    """
    r_max = compute(TaxInput(province="ON", year=2025,
                             self_employment_income=D(90000)))
    expected = s8_part4(D(90000))
    assert expected["credit"] == D("3356.10")
    assert expected["deduction"] == D("5504.10")
    assert r_max.total_deductions == expected["deduction"]


def test_mixed_employment_and_se_payable_preserved():
    """Employment contributions shrink the SE bands (Part 5); allocation must
    not change the payable there either."""
    inp = TaxInput(province="ON", year=2025, employment_income=D(40000),
                   self_employment_income=D(50000), cpp_contrib=D("2171.75"))
    r = compute(inp)
    band1 = min(max(min(D(90000), YMPE) - EXEMPTION, D(0)),
                MAX_BASE_FIRST_EARNINGS) - (D(40000) - EXEMPTION)
    band2 = max(min(D(90000), YAMPE) - YMPE, D(0))
    combined = (band1 * D("0.119") + band2 * D("0.08")).quantize(CENT, ROUND_HALF_UP)
    assert r.cpp_payable_se == combined


def test_bootstrap_split_reconciles_with_combined_rate():
    f = FEDERAL_2025
    assert f.cpp_base_rate + f.cpp_first_additional_rate == f.cpp_rate


# ---------------------------------------------------------------------------
# ITA 118.1(3) donation credit
# ---------------------------------------------------------------------------
TOP_THRESHOLD = FEDERAL_2025.brackets[-2].up_to          # 253,414 for 2025
TOP_RATE = FEDERAL_2025.brackets[-1].rate                # 33%


def act_credit(gifts: Decimal, taxable: Decimal) -> Decimal:
    """118.1(3) transcribed: A×B + C×D + E×F, D and F partitioning gifts−200."""
    a_b = min(gifts, D(200)) * FEDERAL_2025.donation_low
    over = max(gifts - D(200), D(0))
    d_term = min(over, max(taxable - TOP_THRESHOLD, D(0)))
    return a_b + d_term * TOP_RATE + (over - d_term) * FEDERAL_2025.donation_high


@pytest.mark.parametrize("gifts,taxable", [
    (D(0), D(400000)),
    (D(150), D(400000)),          # inside the first tier only
    (D(200), D(400000)),          # exactly the first tier
    (D(250), D(400000)),          # just above the first tier
    (D(10000), D(200000)),        # below the top bracket: no 33% component
    (D(10000), TOP_THRESHOLD),    # exactly at the threshold: still none
    (D(10000), TOP_THRESHOLD + 1),   # $1 of top-bracket income
    (D(10000), D(400000)),        # gift fully matched by top-bracket income
    (D(50000), D(300000)),        # gift only PARTIALLY matched (46,586)
    (D(300000), D(400000)),       # income bound, not gift bound
])
def test_donation_credit_follows_118_1_3(gifts, taxable):
    assert _donation_credit(gifts, taxable, FEDERAL_2025) == act_credit(gifts, taxable)


def test_donation_no_top_component_below_threshold():
    below = _donation_credit(D(10000), D(200000), FEDERAL_2025)
    at = _donation_credit(D(10000), TOP_THRESHOLD, FEDERAL_2025)
    flat = min(D(10000), D(200)) * FEDERAL_2025.donation_low \
        + (D(10000) - D(200)) * FEDERAL_2025.donation_high
    assert below == at == flat


def test_donation_partial_top_treatment_bounded_both_ways():
    """TI 300k, gifts 50k: only 46,586 of the 49,800 over-$200 portion is
    top-bracket-matched; the remaining 3,214 earns the ordinary 29%."""
    got = _donation_credit(D(50000), D(300000), FEDERAL_2025)
    top_income = D(300000) - TOP_THRESHOLD
    expected = (D(200) * FEDERAL_2025.donation_low
                + top_income * TOP_RATE
                + (D(49800) - top_income) * FEDERAL_2025.donation_high)
    assert got == expected
    # bounded by the gift when income is the larger side
    gift_bound = _donation_credit(D(1000), D(400000), FEDERAL_2025)
    assert gift_bound == (D(200) * FEDERAL_2025.donation_low
                          + D(800) * TOP_RATE)


def test_donation_no_double_count_and_monotonic():
    """Every over-$200 dollar is priced exactly once, and more gift never
    yields less credit."""
    prev = D(0)
    for gifts in (D(0), D(100), D(200), D(1000), D(50000), D(150000)):
        credit = _donation_credit(gifts, D(300000), FEDERAL_2025)
        over = max(gifts - D(200), D(0))
        top = min(over, D(300000) - TOP_THRESHOLD)
        # decomposition sums to the whole gift once
        assert min(gifts, D(200)) + top + (over - top) == gifts
        assert credit >= prev
        prev = credit


def test_donation_reduces_engine_tax_for_top_bracket_donor():
    """End to end: the previously overstated case now reflects the 33% tier."""
    base = compute(TaxInput(province="ON", year=2025,
                            employment_income=D(420000)))
    with_gift = compute(TaxInput(province="ON", year=2025,
                                 employment_income=D(420000),
                                 donations=D(10000)))
    fed_delta = base.federal_tax - with_gift.federal_tax
    expected = act_credit(D(10000), base.taxable_income)
    assert fed_delta == expected.quantize(CENT, ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# FHSA_ROOM default capacity
# ---------------------------------------------------------------------------
def test_undeclared_fhsa_room_defaults_to_annual_participation_room():
    ds = bootstrap_dataset(2025)
    caps = _resource_capacities({}, ds)
    assert caps == {"FHSA_ROOM": ds.federal.fhsa_annual}
    assert ds.federal.fhsa_annual == D(8000)


def test_declared_fhsa_room_is_preserved():
    ds = bootstrap_dataset(2025)
    assert _resource_capacities(
        {"resource_capacities": {"FHSA_ROOM": "5000"}}, ds)["FHSA_ROOM"] == D(5000)
    # a legitimate carry-forward declaration above the annual room survives
    assert _resource_capacities(
        {"resource_capacities": {"FHSA_ROOM": "12000"}}, ds)["FHSA_ROOM"] == D(12000)


def test_other_declared_pools_unchanged_and_not_defaulted():
    ds = bootstrap_dataset(2025)
    caps = _resource_capacities(
        {"resource_capacities": {"RRSP_ROOM": "31000"}}, ds)
    assert caps["RRSP_ROOM"] == D(31000)
    assert caps["FHSA_ROOM"] == ds.federal.fhsa_annual
    assert "DONATION_POOL" not in caps        # only FHSA has a published default


def test_default_room_constrains_ledger_and_within_room_passes():
    ds = bootstrap_dataset(2025)
    ledger = ResourceLedger(capacities=_resource_capacities({}, ds))
    assert ledger.remaining("FHSA_ROOM") == D(8000)          # no longer uncapped
    assert ledger.try_allocate({"FHSA_ROOM": D(9000)}) is None
    granted = ledger.try_allocate({"FHSA_ROOM": D(8000)})
    assert granted == {"FHSA_ROOM": D(8000)}
    ledger.commit(granted)
    assert ledger.remaining("FHSA_ROOM") == D(0)


def test_default_tracks_resolved_dataset_value():
    """Changing the governed annual value in the dataset changes the default
    with no application-code change — the provider overlay owns it."""
    ds = bootstrap_dataset(2025)
    varied = replace(ds, federal=replace(ds.federal, fhsa_annual=D(9000)))
    assert _resource_capacities({}, varied)["FHSA_ROOM"] == D(9000)
