"""Tax constants as data — the pure engine receives these; it never fetches.

In production a `TaxDataProvider` loads brackets/credits from `tax_kb.*`. This
in-memory 2025 dataset (federal + the four verified provinces) bootstraps the
engine and backs the deterministic golden-case tests. Values mirror the
validated figures (July-2025 federal cut → blended 14.5%).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# Declared version of this in-code reference dataset (IOE decision D-11).
# Because the dataset lives in code rather than in the database, nothing else can
# detect that it changed. Bump this whenever any value below is edited; the IOE
# pins it in a run's version manifest and hashes the dataset's content beside it,
# so a corrected bracket makes historical replay report DRIFTED instead of
# silently producing a different number.
#
# Additive and inert: no calculation reads this constant.
#
# 2025.3.0 — three calculation behaviours changed to match registered
# authority, so results differ for affected filers and sealed runs from
# earlier versions must report DRIFTED rather than silently replaying:
#   1. Self-employed CPP allocation now follows Schedule 8 (5000-S8 E (25))
#      Part 4 lines 15/17 and ITA 60(e)/(e.1)/118.7: the credit is HALF OF THE
#      BASE contribution only, and the first and second additional
#      contributions are fully deductible. `cpp_base_rate` and
#      `cpp_first_additional_rate` were added to carry the split the schedule
#      states; the amount payable is unchanged (0.0495 + 0.01 = 0.0595).
#   2. The federal donation credit now includes the ITA 118.1(3) C x D term —
#      the highest individual percentage on the top-bracket-matched portion of
#      gifts over the first tier. Top-bracket donors previously had tax
#      overstated.
#   3. `fhsa_annual` is now consumed: it is the default FHSA_ROOM capacity for
#      a run whose user declared none, and the provider can overlay it from a
#      published FHSA_ANNUAL_PARTICIPATION_ROOM constant.
#
# 2025.2.0 — the second additional CPP contribution (CPP2) became computable.
# `cpp2_max_pensionable` and `cpp2_rate` were added and `compute` now produces a
# CPP2 contribution for a self-employed filer above the year's maximum
# pensionable earnings, where it previously produced nothing. That changes the
# answer for those filers, which is exactly the condition this constant exists
# to signal: a run sealed under 2025.1.0 now reports DRIFTED on verification
# rather than quietly replaying to a different number.
REFERENCE_DATA_VERSION = "2025.3.0"


def D(x: int | str | Decimal) -> Decimal:
    return Decimal(str(x))


def _describe_brackets(brackets: list[Bracket]) -> list[dict]:
    return [{"up_to": None if b.up_to is None else str(b.up_to),
             "rate": str(b.rate)} for b in brackets]


@dataclass(frozen=True)
class Bracket:
    up_to: Decimal | None  # None = top bracket (∞)
    rate: Decimal


@dataclass(frozen=True)
class ProvincialData:
    name: str
    brackets: list[Bracket]
    bpa: Decimal
    credit_rate: Decimal
    abatement: Decimal = D(0)
    surtax: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (over, rate)
    # The final band is open-ended: `None` upper bound means 'and above'.
    health_premium: list[tuple[Decimal | None, Decimal]] = field(default_factory=list)  # (up_to, amount)


@dataclass(frozen=True)
class FederalData:
    brackets: list[Bracket]
    bpa_max: Decimal
    bpa_min: Decimal
    bpa_phase_start: Decimal
    bpa_phase_end: Decimal
    credit_rate: Decimal
    canada_employment: Decimal
    cpp_max_pensionable: Decimal
    cpp_exemption: Decimal
    cpp_rate: Decimal
    #: The split Schedule 8 states inside the combined employee rate:
    #: `cpp_rate == cpp_base_rate + cpp_first_additional_rate`. The split is
    #: load-bearing, not decorative — the BASE portion is half-credited and
    #: half-deducted for the self-employed while both additional portions are
    #: fully deducted (5000-S8 E (25) Part 4 lines 15/17; ITA 60(e)/(e.1),
    #: 118.7), so an engine holding only the combined rate cannot allocate.
    #: Self-employed rates (9.9%/2%) are these doubled, derived not stored.
    cpp_base_rate: Decimal
    cpp_first_additional_rate: Decimal
    cpp_max: Decimal
    #: CPP2 applies to the band ABOVE `cpp_max_pensionable` and up to the year's
    #: ADDITIONAL maximum pensionable earnings (YAMPE). `cpp2_max` is the
    #: maximum a single employee or employer pays; the self-employed maximum is
    #: twice it, which the engine derives rather than storing again.
    cpp2_max: Decimal
    cpp2_max_pensionable: Decimal
    cpp2_rate: Decimal
    ei_max_insurable: Decimal
    ei_rate: Decimal
    ei_max: Decimal
    medical_pct: Decimal
    medical_cap: Decimal
    donation_low: Decimal
    donation_high: Decimal
    eligible_div_gross_up: Decimal
    eligible_div_dtc: Decimal
    non_eligible_div_gross_up: Decimal
    non_eligible_div_dtc: Decimal
    capital_gains_inclusion: Decimal
    fhsa_annual: Decimal


FEDERAL_2025 = FederalData(
    brackets=[
        Bracket(D(57375), D("0.145")), Bracket(D(114750), D("0.205")),
        Bracket(D(177882), D("0.26")), Bracket(D(253414), D("0.29")),
        Bracket(None, D("0.33")),
    ],
    bpa_max=D(16129), bpa_min=D(14538), bpa_phase_start=D(177882), bpa_phase_end=D(253414),
    credit_rate=D("0.145"), canada_employment=D(1471),
    cpp_max_pensionable=D(71300), cpp_exemption=D(3500), cpp_rate=D("0.0595"),
    cpp_base_rate=D("0.0495"), cpp_first_additional_rate=D("0.01"),
    cpp_max=D("4034.10"), cpp2_max=D(396),
    cpp2_max_pensionable=D(81200), cpp2_rate=D("0.04"),
    ei_max_insurable=D(65700), ei_rate=D("0.0164"), ei_max=D("1077.48"),
    medical_pct=D("0.03"), medical_cap=D(2834),
    donation_low=D("0.145"), donation_high=D("0.29"),
    eligible_div_gross_up=D("0.38"), eligible_div_dtc=D("0.150198"),
    non_eligible_div_gross_up=D("0.15"), non_eligible_div_dtc=D("0.090301"),
    capital_gains_inclusion=D("0.5"), fhsa_annual=D(8000),
)

PROVINCES_2025: dict[str, ProvincialData] = {
    "ON": ProvincialData(
        name="Ontario",
        brackets=[
            Bracket(D(52886), D("0.0505")), Bracket(D(105775), D("0.0915")),
            Bracket(D(150000), D("0.1116")), Bracket(D(220000), D("0.1216")),
            Bracket(None, D("0.1316")),
        ],
        bpa=D(12747), credit_rate=D("0.0505"),
        surtax=[(D(5710), D("0.20")), (D(7307), D("0.36"))],
        health_premium=[
            (D(20000), D(0)), (D(36000), D(300)), (D(48000), D(450)),
            (D(72000), D(600)), (D(200000), D(750)), (None, D(900)),
        ],
    ),
    "BC": ProvincialData(
        name="British Columbia",
        brackets=[
            Bracket(D(49279), D("0.0506")), Bracket(D(98560), D("0.077")),
            Bracket(D(113158), D("0.105")), Bracket(D(137407), D("0.1229")),
            Bracket(D(186306), D("0.147")), Bracket(D(259829), D("0.168")),
            Bracket(None, D("0.205")),
        ],
        bpa=D(12932), credit_rate=D("0.0506"),
    ),
    "AB": ProvincialData(
        name="Alberta",
        brackets=[
            Bracket(D(60000), D("0.08")), Bracket(D(151234), D("0.10")),
            Bracket(D(181481), D("0.12")), Bracket(D(241974), D("0.13")),
            Bracket(D(362961), D("0.14")), Bracket(None, D("0.15")),
        ],
        bpa=D(22323), credit_rate=D("0.10"),
    ),
    "QC": ProvincialData(
        name="Quebec",
        brackets=[
            Bracket(D(53255), D("0.14")), Bracket(D(106495), D("0.19")),
            Bracket(D(129590), D("0.24")), Bracket(None, D("0.2575")),
        ],
        bpa=D(18571), credit_rate=D("0.14"), abatement=D("0.165"),
    ),
}


# ---------------------------------------------------------------------------
# Resolved datasets
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TaxDataset:
    """One coherent set of tax constants, resolved for one tax year.

    The engine receives one of these and never fetches. Which jurisdictions came
    from governed reference data is part of the dataset's identity on purpose: a
    dataset whose Ontario brackets came from the registry is not the same
    dataset as one whose Ontario brackets came from the constants below, even
    where the numbers happen to agree, and a snapshot that could not tell them
    apart would let the source of a published figure change unnoticed.

    Lives here rather than beside the provider so that the pure engine can take
    a dataset without importing the database layer.
    """

    tax_year: int
    federal: FederalData
    provinces: dict[str, ProvincialData]
    governed_jurisdictions: frozenset[str] = frozenset()
    #: Which CALC_CONSTANT codes were overlaid from governed reference data.
    #: Part of the dataset's identity for the same reason the jurisdictions
    #: are: a CPP rate that came from a published constant is not the same
    #: fact as one that came from the constants below, even where the numbers
    #: agree, and a snapshot that could not tell them apart would let the
    #: source of a published figure change unnoticed.
    governed_constants: frozenset[str] = frozenset()
    #: Identifies the in-code portion, so editing the constants above still
    #: invalidates replay exactly as it does today.
    bootstrap_version: str = REFERENCE_DATA_VERSION

    @property
    def is_fully_bootstrap(self) -> bool:
        return not self.governed_jurisdictions and not self.governed_constants

    def describe(self) -> dict:
        """The content a snapshot pins, as plain comparable values."""
        return {
            "tax_year": self.tax_year,
            "bootstrap_version": self.bootstrap_version,
            "governed_jurisdictions": sorted(self.governed_jurisdictions),
            "federal": _describe_brackets(self.federal.brackets),
            "provinces": {code: _describe_brackets(self.provinces[code].brackets)
                          for code in sorted(self.provinces)},
        }


def bootstrap_dataset(tax_year: int) -> TaxDataset:
    """The in-code dataset, unchanged, labelled for what it is."""
    return TaxDataset(tax_year=tax_year, federal=FEDERAL_2025,
                      provinces=dict(PROVINCES_2025),
                      governed_jurisdictions=frozenset())
