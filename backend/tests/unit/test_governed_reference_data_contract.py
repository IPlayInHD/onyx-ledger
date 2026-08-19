"""The governed-reference-data boundary, without a database.

Two things are proved here. First, that routing the engine's constants through
a resolved dataset changed no arithmetic: the same data still yields the same
result, to the cent. Second, that the boundary is not decorative — a dataset
carrying different brackets moves every number that depends on them, so a test
asserting equivalence cannot be passing merely because the dataset is ignored.

The conversion from governed rows to the engine's ladder is checked hard. The
engine's `Bracket` carries only an upper bound and relies on each band starting
where the last ended, so a governed table with a gap, an overlap or an unbounded
middle band would silently tax income at the wrong rate rather than fail.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from app.services.ioe.portfolio.eligibility import engine_facts_for_dataset
from app.services.ioe.portfolio.service import engine_evaluator_for, inputs_from
from app.services.tax_engine.core.data import (
    Bracket,
    bootstrap_dataset,
)
from app.services.tax_engine.core.engine import TaxInput, compute
from app.services.tax_engine.core.provider import (
    ReferenceDataError,
    _to_engine_brackets,
)


@dataclasses.dataclass
class _Row:
    """The two bounds a governed bracket row carries."""

    ordinal: int
    lower_bound: Decimal
    upper_bound: Decimal | None
    rate: Decimal


def _rows(*bands: tuple[str, str | None, str]) -> list[_Row]:
    return [
        _Row(i + 1, Decimal(lo), None if hi is None else Decimal(hi), Decimal(rate))
        for i, (lo, hi, rate) in enumerate(bands)
    ]


WELL_FORMED = (("0", "57375", "0.145"), ("57375", "114750", "0.205"),
               ("114750", None, "0.26"))


def _input(income: str = "300000") -> TaxInput:
    return TaxInput(province="ON", year=2025, employment_income=Decimal(income))


# --------------------------------------------------------------------------
# Equivalence: same data in, same numbers out
# --------------------------------------------------------------------------
def test_passing_the_bootstrap_dataset_explicitly_changes_no_number():
    """The wiring must be a no-op when the data is the data it always was."""
    inp = _input()
    implicit = compute(inp)
    explicit = compute(inp, bootstrap_dataset(2025))
    assert implicit == explicit


@pytest.mark.parametrize("income", ["0", "45000", "57375", "150000", "300000"])
def test_equivalence_holds_across_the_bracket_ladder(income):
    inp = _input(income)
    assert compute(inp) == compute(inp, bootstrap_dataset(2025))


def test_a_different_dataset_moves_the_answer():
    """Non-vacuity. If this passed, the equivalence tests above prove nothing."""
    base = bootstrap_dataset(2025)
    doubled = dataclasses.replace(
        base,
        federal=dataclasses.replace(
            base.federal,
            brackets=[Bracket(up_to=b.up_to, rate=b.rate * 2)
                      for b in base.federal.brackets]),
        governed_jurisdictions=frozenset({"FED"}))
    inp = _input()
    assert compute(inp, doubled).federal_tax != compute(inp, base).federal_tax


# --------------------------------------------------------------------------
# The bound callables carry the dataset they were built with
# --------------------------------------------------------------------------
def test_the_cost_evaluator_is_bound_to_its_dataset():
    """A candidate's cost must come from the run's tax law, not the module's."""
    base = bootstrap_dataset(2025)
    altered = dataclasses.replace(
        base,
        federal=dataclasses.replace(
            base.federal,
            brackets=[Bracket(up_to=None, rate=Decimal("0.9"))]),
        governed_jurisdictions=frozenset({"FED"}))
    inputs = inputs_from(_input())
    assert engine_evaluator_for(altered)(inputs) != engine_evaluator_for(base)(inputs)


def test_the_recheck_fact_map_is_bound_to_its_dataset():
    """Eligibility must be re-decided on the same tax law the run costed with."""
    base = bootstrap_dataset(2025)
    altered = dataclasses.replace(
        base,
        federal=dataclasses.replace(
            base.federal,
            brackets=[Bracket(up_to=None, rate=Decimal("0.9"))]),
        governed_jurisdictions=frozenset({"FED"}))
    inputs = inputs_from(_input())
    assert (engine_facts_for_dataset(altered)(inputs)["derived.marginal_rate"]
            != engine_facts_for_dataset(base)(inputs)["derived.marginal_rate"])


# --------------------------------------------------------------------------
# Governed rows → engine ladder, checked rather than trusted
# --------------------------------------------------------------------------
def test_a_well_formed_governed_table_converts():
    ladder = _to_engine_brackets(_rows(*WELL_FORMED), "FED 2025")
    assert [b.up_to for b in ladder] == [Decimal("57375"), Decimal("114750"), None]
    assert [b.rate for b in ladder] == [Decimal("0.145"), Decimal("0.205"),
                                        Decimal("0.26")]


def test_conversion_preserves_exact_decimals():
    ladder = _to_engine_brackets(_rows(("0", "1000", "0.0505"),
                                       ("1000", None, "0.1316")), "ON 2025")
    assert ladder[0].rate == Decimal("0.0505")
    assert str(ladder[1].rate) == "0.1316"


@pytest.mark.parametrize(("bands", "because"), [
    ((("0", "100", "0.1"), ("200", None, "0.2")), "a gap between bands"),
    ((("0", "300", "0.1"), ("200", None, "0.2")), "an overlap between bands"),
    ((("0", None, "0.1"), ("100", None, "0.2")), "an open band that is not last"),
    ((("0", "100", "0.1"), ("100", "200", "0.2")), "a bounded top band"),
    ((("50", "100", "0.1"), ("100", None, "0.2")), "a first band above zero"),
])
def test_a_malformed_governed_table_is_refused(bands, because):
    """Fail closed. A broken table must never become a plausible ladder."""
    with pytest.raises(ReferenceDataError):
        _to_engine_brackets(_rows(*bands), f"BAD 2025 ({because})")


def test_an_empty_governed_table_is_refused():
    with pytest.raises(ReferenceDataError):
        _to_engine_brackets([], "EMPTY 2025")


# --------------------------------------------------------------------------
# What a snapshot pins
# --------------------------------------------------------------------------
def test_the_dataset_description_covers_provinces():
    """The artifact that pins the engine's data omitted provinces entirely.

    It read `engine_data.PROVINCES`, which does not exist — the module defines
    `PROVINCES_2025` — so the `getattr` default meant a changed Ontario rate was
    invisible to integrity verification.
    """
    described = bootstrap_dataset(2025).describe()
    assert "ON" in described["provinces"]
    assert described["provinces"]["ON"], "Ontario brackets must be described"


def test_the_description_distinguishes_governed_from_bootstrap():
    """Source is part of identity, not a label beside it."""
    plain = bootstrap_dataset(2025)
    governed = dataclasses.replace(plain,
                                   governed_jurisdictions=frozenset({"FED"}))
    assert plain.describe() != governed.describe()
    assert plain.is_fully_bootstrap
    assert not governed.is_fully_bootstrap


def test_a_changed_province_bracket_changes_the_description():
    """Otherwise drift detection cannot see a provincial correction."""
    base = bootstrap_dataset(2025)
    moved = dataclasses.replace(
        base,
        provinces={**base.provinces,
                   "ON": dataclasses.replace(
                       base.provinces["ON"],
                       brackets=[Bracket(up_to=None, rate=Decimal("0.5"))])})
    assert base.describe()["provinces"]["ON"] != moved.describe()["provinces"]["ON"]


def test_the_dataset_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        bootstrap_dataset(2025).tax_year = 2026     # type: ignore[misc]


def test_compute_never_opens_a_socket():
    """The engine takes data and does no I/O — asserted, not assumed."""
    import socket

    opened: list[object] = []
    real = socket.socket.connect

    def counting(self, *a, **kw):                    # pragma: no cover
        opened.append(a)
        return real(self, *a, **kw)

    socket.socket.connect = counting                 # type: ignore[method-assign]
    try:
        result = compute(_input(), bootstrap_dataset(2025))
    finally:
        socket.socket.connect = real                 # type: ignore[method-assign]
    assert result.federal_tax > 0, "must have done real work"
    assert opened == []


def test_dataset_type_is_importable_without_the_database_layer():
    """The pure engine must not gain a SQLAlchemy dependency at import time."""
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import app.services.tax_engine.core.engine;"
        "print('sqlalchemy' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=".", check=True)
    assert out.stdout.strip() == "False", out.stdout
