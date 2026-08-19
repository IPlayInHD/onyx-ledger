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


# ---------------------------------------------------------------------------
# Sealing and replaying governed CONSTANTS
#
# Brackets already had this proof. Constants need their own, because they reach
# the sealed dataset by a different route and because a run may take governed
# constants while every bracket table is still in-code — which is exactly the
# state the first published constants create.
# ---------------------------------------------------------------------------
def _with_constants(tax_year: int, **fields):
    """A dataset whose CPP/EI figures came from governed rows."""
    import dataclasses

    base = bootstrap_dataset(tax_year)
    return dataclasses.replace(
        base,
        federal=dataclasses.replace(base.federal, **fields),
        governed_constants=frozenset(fields),
    )


def test_governed_constants_are_part_of_the_sealed_description():
    from app.services.ioe.snapshot.service import describe_dataset

    plain = describe_dataset(bootstrap_dataset(2025))
    governed = describe_dataset(_with_constants(2025, cpp_rate=Decimal("0.0595")))
    # Absent when there are none, so a bootstrap run describes as it always did.
    assert "governed_constants" not in plain
    assert governed["governed_constants"] == ["cpp_rate"]


def test_a_dataset_with_governed_constants_round_trips_exactly():
    from app.services.ioe.snapshot.service import dataset_from_sealed, describe_dataset

    sealed = describe_dataset(_with_constants(
        2025, cpp_rate=Decimal("0.0595"), ei_rate=Decimal("0.0164")))
    rebuilt = dataset_from_sealed(sealed, 2025)
    assert describe_dataset(rebuilt) == sealed
    assert rebuilt.governed_constants == frozenset({"cpp_rate", "ei_rate"})


def test_replay_uses_the_sealed_constants_not_todays():
    """V1 sealed, V2 arrives, replay V1 — and gets V1.

    The substitution this prevents is silent: both datasets compute, and the
    replayed number would simply be the wrong one.
    """
    from app.services.ioe.snapshot.service import dataset_from_sealed, describe_dataset

    v1 = _with_constants(2025, cpp_rate=Decimal("0.0500"))
    sealed_v1 = describe_dataset(v1)
    # V2 is what the tables would say today. It is never consulted.
    v2 = _with_constants(2025, cpp_rate=Decimal("0.0900"))
    assert describe_dataset(v2) != sealed_v1

    replayed = dataset_from_sealed(sealed_v1, 2025)
    assert replayed.federal.cpp_rate == Decimal("0.0500")

    inp = TaxInput(province="ON", year=2025,
                   self_employment_income=Decimal("80000"))
    assert compute(inp, replayed).cpp_payable_se == compute(inp, v1).cpp_payable_se
    assert compute(inp, replayed).cpp_payable_se != compute(inp, v2).cpp_payable_se


def test_a_constants_only_run_is_not_mistaken_for_a_bootstrap_run():
    """The bootstrap shortcut must test BOTH kinds of governance.

    A run with governed constants and no governed brackets would otherwise take
    the shortcut and be handed today's in-code constants instead of its own.
    """
    from app.services.ioe.snapshot.service import dataset_from_sealed, describe_dataset

    sealed = describe_dataset(_with_constants(2025, cpp_rate=Decimal("0.0500")))
    assert sealed["governed_jurisdictions"] == []       # no governed brackets
    assert dataset_from_sealed(sealed, 2025).federal.cpp_rate == Decimal("0.0500")


def test_tampered_sealed_constants_fail_closed():
    from app.services.ioe.snapshot.service import (
        SealedDatasetUnreadable,
        dataset_from_sealed,
        describe_dataset,
    )

    sealed = describe_dataset(_with_constants(2025, cpp_rate=Decimal("0.0595")))

    # An edited VALUE is refused. The round-trip proof is what catches it: the
    # sealed description carries canonical scale, so a hand-edited "0.0700"
    # re-describes as "0.070000" and no longer equals the bytes it claims to
    # be. Replay stops rather than computing from an approximation of itself.
    tampered = {**sealed, "federal": {**sealed["federal"], "cpp_rate": "0.0700"}}
    with pytest.raises(SealedDatasetUnreadable, match="does not describe back"):
        dataset_from_sealed(tampered, 2025)

    # A REMOVED field cannot be reconstructed at all.
    broken = {**sealed, "federal": {k: val for k, val in sealed["federal"].items()
                                    if k != "cpp_rate"}}
    with pytest.raises(SealedDatasetUnreadable):
        dataset_from_sealed(broken, 2025)

    # Relabelling governed evidence as bootstrap is caught, but NOT here — and
    # it is worth being exact about where. `dataset_from_sealed` deliberately
    # replays a bootstrap run from the in-code constants, which is what lets
    # runs sealed before governed data existed keep replaying at all. So an
    # artifact edited to claim it governed nothing does reconstruct.
    #
    # What makes that safe is the layer above: the artifact's content hash is
    # stored beside it, so any edit to the content changes the hash and
    # `SnapshotService.verify()` reports drift. Tamper-evidence lives in the
    # seal, not in the reader.
    from app.services.ioe.domain import canonical as canon

    relabelled = {**sealed, "governed_constants": []}
    assert dataset_from_sealed(relabelled, 2025).governed_constants == frozenset()
    assert canon.canonical_hash(relabelled) != canon.canonical_hash(sealed)


def test_a_legacy_artifact_without_the_constants_key_still_reconstructs():
    """Runs sealed before governed constants existed keep replaying."""
    from app.services.ioe.snapshot.service import dataset_from_sealed, describe_dataset

    legacy = describe_dataset(bootstrap_dataset(2025))
    assert "governed_constants" not in legacy
    rebuilt = dataset_from_sealed(legacy, 2025)
    assert rebuilt.governed_constants == frozenset()
    assert rebuilt.federal.cpp_rate == bootstrap_dataset(2025).federal.cpp_rate
