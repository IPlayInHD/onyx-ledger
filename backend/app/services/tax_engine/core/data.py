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
REFERENCE_DATA_VERSION = "2025.1.0"


def D(x: int | str | Decimal) -> Decimal:
    return Decimal(str(x))


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
    cpp_max: Decimal
    cpp2_max: Decimal
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
    cpp_max=D("4034.10"), cpp2_max=D(396),
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
