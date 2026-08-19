"""Intra-year effective periods on governed reference data, end to end.

The defect this closes: CRA announces prescribed interest rates per calendar
quarter, and `rules.calc_constant` identified a value by (code, tax_year)
alone, so four quarters of one year collided on one key. B04 could be
registered and cited but not authored. These tests exercise the model that
makes it storable — and, just as importantly, the refusals that keep it from
storing a contradiction.

Every test writes inside a transaction that is ROLLED BACK. `calc_constant` is
guarded by an exclusion constraint over (code, tax_year, period), so residue
from one test would collide with the next rather than merely confuse it.
"""

from __future__ import annotations

import contextlib
import os
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.database.models import CalcConstant
from app.services.tax_engine.core.provider import (
    ReferenceDataError,
    TaxDataProvider,
)
from app.services.tax_kb.authoring import validation as v
from app.services.tax_kb.authoring.codes import ValidationCode
from app.services.tax_kb.authoring.spec import ReferenceDataSpec

TAX_YEAR = 2026
Q3_FROM, Q3_TO = date(2026, 7, 1), date(2026, 9, 30)
Q4_FROM, Q4_TO = date(2026, 10, 1), date(2026, 12, 31)
#: A code no seed or other test uses, so these rows cannot collide with content.
CODE = "TEST_PRESCRIBED_RATE_OVERDUE"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _owner_url() -> str:
    parsed = urlparse(os.environ.get("ONYX_DATABASE_URL", ""))
    query = parse_qs(parsed.query)
    database = (parsed.path or "/onyx_test").lstrip("/").split("?")[0]
    host = query.get("host", ["/var/run/postgresql"])[0]
    port = query.get("port", ["5432"])[0]
    return (f"postgresql+asyncpg://onyx_migrator@/{database}"
            f"?host={host}&port={port}")


@contextlib.asynccontextmanager
async def _scratch():
    engine = create_async_engine(_owner_url(), poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = factory()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _clear(session: AsyncSession) -> None:
    await session.execute(delete(CalcConstant).where(CalcConstant.code == CODE))
    await session.flush()


async def _add(session: AsyncSession, value: str, *,
               start: date | None = None, end: date | None = None,
               code: str = CODE, unit: str = "ratio") -> CalcConstant:
    row = CalcConstant(code=code, tax_year=TAX_YEAR, value=Decimal(value),
                       unit=unit, effective_from=start, effective_to=end)
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Storage: what may coexist, and what may not
# ---------------------------------------------------------------------------
async def test_two_non_overlapping_quarters_of_one_year_coexist():
    """The whole point. Same code, same tax year, different periods."""
    async with _scratch() as session:
        await _clear(session)
        await _add(session, "0.07", start=Q3_FROM, end=Q3_TO)
        await _add(session, "0.08", start=Q4_FROM, end=Q4_TO)
        rows = list(await session.scalars(
            select(CalcConstant).where(CalcConstant.code == CODE)))
        assert len(rows) == 2
        assert {r.value for r in rows} == {Decimal("0.07"), Decimal("0.08")}


async def test_adjacent_periods_do_not_count_as_overlapping():
    """Q3 ends 09-30 and Q4 starts 10-01 with no gap and no overlap.

    Both bounds are inclusive, so an off-by-one in the range would make the
    ordinary case — a year cut into consecutive quarters — impossible to store.
    """
    async with _scratch() as session:
        await _clear(session)
        await _add(session, "0.07", start=Q3_FROM, end=date(2026, 9, 30))
        await _add(session, "0.08", start=date(2026, 10, 1), end=Q4_TO)
        assert len(list(await session.scalars(
            select(CalcConstant).where(CalcConstant.code == CODE)))) == 2


async def test_overlapping_periods_are_refused_by_the_database():
    async with _scratch() as session:
        await _clear(session)
        await _add(session, "0.07", start=Q3_FROM, end=Q3_TO)
        with pytest.raises(IntegrityError):
            await _add(session, "0.08", start=date(2026, 9, 30), end=Q4_TO)


async def test_an_annual_row_excludes_a_period_row_for_the_same_code_and_year():
    """"The 2026 value is X" and "the Q3 2026 value is Y" contradict.

    An annual row is unbounded, so the exclusion constraint catches this
    without needing a rule of its own.
    """
    async with _scratch() as session:
        await _clear(session)
        await _add(session, "0.07")                      # annual
        with pytest.raises(IntegrityError):
            await _add(session, "0.08", start=Q3_FROM, end=Q3_TO)


async def test_two_annual_rows_are_still_refused():
    """The exclusion constraint has to subsume the unique constraint it replaced."""
    async with _scratch() as session:
        await _clear(session)
        await _add(session, "0.07")
        with pytest.raises(IntegrityError):
            await _add(session, "0.08")


async def test_a_period_running_backwards_is_refused():
    async with _scratch() as session:
        await _clear(session)
        with pytest.raises(IntegrityError):
            await _add(session, "0.07", start=Q3_TO, end=Q3_FROM)


# ---------------------------------------------------------------------------
# Resolution boundaries
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (date(2026, 6, 30), None),          # the day before it starts
        (date(2026, 7, 1), "0.07"),         # exactly the first day
        (date(2026, 9, 30), "0.07"),        # exactly the last day
        (date(2026, 10, 1), "0.08"),        # the transition to the next period
        (date(2026, 12, 31), "0.08"),       # the last day of the next period
    ],
)
async def test_resolution_selects_by_effective_date(as_of, expected):
    """Both bounds inclusive, and the transition lands on the right side."""
    async with _scratch() as session:
        await _clear(session)
        await _add(session, "0.07", start=Q3_FROM, end=Q3_TO)
        await _add(session, "0.08", start=Q4_FROM, end=Q4_TO)
        eligible = list(await session.scalars(
            select(CalcConstant).where(
                CalcConstant.code == CODE,
                CalcConstant.effective_from <= as_of,
                CalcConstant.effective_to >= as_of)))
        if expected is None:
            assert eligible == []
        else:
            assert len(eligible) == 1
            assert eligible[0].value == Decimal(expected)


async def test_without_a_date_a_periodised_constant_is_not_applied():
    """No date is not "pick one". Resolution refuses to guess.

    Uses a code the engine actually reads, so a wrong answer here would reach a
    calculation rather than stopping at a query.
    """
    async with _scratch() as session:
        await session.execute(delete(CalcConstant).where(
            CalcConstant.code == "CPP_CONTRIBUTION_RATE",
            CalcConstant.tax_year == TAX_YEAR))
        await _add(session, "0.0123", code="CPP_CONTRIBUTION_RATE",
                   start=Q3_FROM, end=Q3_TO)

        undated = await TaxDataProvider(session).resolve(TAX_YEAR)
        assert "CPP_CONTRIBUTION_RATE" not in undated.governed_constants
        assert undated.federal.cpp_rate == Decimal("0.0595")   # bootstrap, untouched

        dated = await TaxDataProvider(session).resolve(TAX_YEAR, as_of=Q3_FROM)
        assert "CPP_CONTRIBUTION_RATE" in dated.governed_constants
        assert dated.federal.cpp_rate == Decimal("0.0123")


# ---------------------------------------------------------------------------
# Annual compatibility
# ---------------------------------------------------------------------------
async def test_an_annual_row_needs_no_date_and_is_unaffected_by_one():
    async with _scratch() as session:
        await session.execute(delete(CalcConstant).where(
            CalcConstant.code == "EI_PREMIUM_RATE",
            CalcConstant.tax_year == TAX_YEAR))
        await _add(session, "0.0164", code="EI_PREMIUM_RATE")

        for as_of in (None, date(2026, 1, 1), date(2026, 12, 31)):
            dataset = await TaxDataProvider(session).resolve(TAX_YEAR, as_of=as_of)
            assert dataset.federal.ei_rate == Decimal("0.0164")
            assert "EI_PREMIUM_RATE" in dataset.governed_constants


def test_an_annual_spec_omits_the_period_keys_entirely():
    """The mechanism that keeps annual identity stable.

    Emitting the two keys unconditionally would have changed the identity of
    every annual object already validated and staged — including the hashes
    publication rebinds against. That the 51 constants staged by the completed
    ingestion batches still hash to their recorded values was verified against
    those recorded hashes when this entry was written; what is pinned HERE is
    the property that made it true, plus a golden hash so a later change cannot
    move annual identity without a test saying so.
    """
    annual = ReferenceDataSpec.read({
        "kind": "CALC_CONSTANT", "key": "MEDICAL_FLOOR_RATE", "tax_year": 2025,
        "value": "0.03", "unit": "ratio", "citation_ids": []})
    canonical = annual.as_canonical()
    assert "effective_from" not in canonical
    assert "effective_to" not in canonical
    assert annual.spec_identity() == (
        "4b464fdfe561836a036c4020664403cad1094ef187089e3214d4d516ca20a44c")


def test_a_period_changes_content_identity():
    base = {"kind": "CALC_CONSTANT", "key": CODE, "tax_year": TAX_YEAR,
            "value": "0.07", "unit": "ratio", "citation_ids": []}
    plain = ReferenceDataSpec.read(base)
    q3 = ReferenceDataSpec.read({**base, "effective_from": "2026-07-01",
                                 "effective_to": "2026-09-30"})
    q4 = ReferenceDataSpec.read({**base, "effective_from": "2026-10-01",
                                 "effective_to": "2026-12-31"})
    assert len({plain.spec_identity(), q3.spec_identity(), q4.spec_identity()}) == 3


# ---------------------------------------------------------------------------
# Validation refusals, with coded findings rather than IntegrityErrors
# ---------------------------------------------------------------------------
def _codes(findings) -> set[str]:
    return {str(f.code) for f in findings}


def _ctx(**kw) -> v.KnowledgeContext:
    return v.KnowledgeContext(known_years=frozenset({TAX_YEAR}), **kw)


def test_a_half_open_period_is_refused():
    spec = ReferenceDataSpec.read({
        "kind": "CALC_CONSTANT", "key": CODE, "tax_year": TAX_YEAR,
        "value": "0.07", "unit": "ratio", "citation_ids": [],
        "effective_from": "2026-07-01"})
    findings = v.validate_reference_data(spec, _ctx())
    assert str(ValidationCode.INVALID_EFFECTIVE_PERIOD) in _codes(findings)


def test_an_inverted_period_is_refused():
    spec = ReferenceDataSpec.read({
        "kind": "CALC_CONSTANT", "key": CODE, "tax_year": TAX_YEAR,
        "value": "0.07", "unit": "ratio", "citation_ids": [],
        "effective_from": "2026-09-30", "effective_to": "2026-07-01"})
    assert str(ValidationCode.INVALID_EFFECTIVE_PERIOD) in _codes(
        v.validate_reference_data(spec, _ctx()))


def test_a_period_reaching_outside_its_tax_year_is_refused():
    spec = ReferenceDataSpec.read({
        "kind": "CALC_CONSTANT", "key": CODE, "tax_year": TAX_YEAR,
        "value": "0.07", "unit": "ratio", "citation_ids": [],
        "effective_from": "2026-07-01", "effective_to": "2027-01-31"})
    assert str(ValidationCode.EFFECTIVE_PERIOD_OUTSIDE_TAX_YEAR) in _codes(
        v.validate_reference_data(spec, _ctx()))


def test_publishing_over_an_already_published_period_is_refused():
    published = v.PublishedReferenceData(
        "CALC_CONSTANT", CODE, TAX_YEAR, Q3_FROM, Q3_TO)
    clashing = ReferenceDataSpec.read({
        "kind": "CALC_CONSTANT", "key": CODE, "tax_year": TAX_YEAR,
        "value": "0.09", "unit": "ratio", "citation_ids": [],
        "effective_from": "2026-08-01", "effective_to": "2026-11-30"})
    assert str(ValidationCode.REFERENCE_DATA_ALREADY_PUBLISHED) in _codes(
        v.validate_reference_data(
            clashing, _ctx(published_reference_data=frozenset({published}))))


def test_a_later_quarter_beside_a_published_one_is_not_a_republication():
    """The refusal above must not also refuse the case periods exist for."""
    published = v.PublishedReferenceData(
        "CALC_CONSTANT", CODE, TAX_YEAR, Q3_FROM, Q3_TO)
    q4 = ReferenceDataSpec.read({
        "kind": "CALC_CONSTANT", "key": CODE, "tax_year": TAX_YEAR,
        "value": "0.08", "unit": "ratio", "citation_ids": [],
        "effective_from": "2026-10-01", "effective_to": "2026-12-31"})
    assert str(ValidationCode.REFERENCE_DATA_ALREADY_PUBLISHED) not in _codes(
        v.validate_reference_data(
            q4, _ctx(published_reference_data=frozenset({published}))))


def test_overlap_is_symmetric_and_inclusive():
    assert v.periods_overlap(Q3_FROM, Q3_TO, Q3_FROM, Q3_TO)
    assert not v.periods_overlap(Q3_FROM, Q3_TO, Q4_FROM, Q4_TO)
    assert not v.periods_overlap(Q4_FROM, Q4_TO, Q3_FROM, Q3_TO)
    # Touching on a single day IS an overlap: both bounds are inclusive.
    assert v.periods_overlap(Q3_FROM, Q3_TO, Q3_TO, Q4_TO)
    # An annual object covers everything, in both directions.
    assert v.periods_overlap(None, None, Q3_FROM, Q3_TO)
    assert v.periods_overlap(Q3_FROM, Q3_TO, None, None)


# ---------------------------------------------------------------------------
# Governed constant overlay: applied, recorded, and fail-closed
# ---------------------------------------------------------------------------
async def test_a_governed_constant_reaches_the_engine_and_is_recorded():
    async with _scratch() as session:
        await session.execute(delete(CalcConstant).where(
            CalcConstant.code == "CPP_MAX_PENSIONABLE_EARNINGS",
            CalcConstant.tax_year == TAX_YEAR))
        await _add(session, "74600", code="CPP_MAX_PENSIONABLE_EARNINGS",
                   unit="CAD")
        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)
        assert dataset.federal.cpp_max_pensionable == Decimal("74600")
        assert "CPP_MAX_PENSIONABLE_EARNINGS" in dataset.governed_constants
        assert not dataset.is_fully_bootstrap


async def test_a_constant_published_in_the_wrong_unit_raises():
    """A rate stored as CAD is not a value to use and not one to ignore."""
    async with _scratch() as session:
        await session.execute(delete(CalcConstant).where(
            CalcConstant.code == "EI_PREMIUM_RATE",
            CalcConstant.tax_year == TAX_YEAR))
        await _add(session, "0.0164", code="EI_PREMIUM_RATE", unit="CAD")
        with pytest.raises(ReferenceDataError, match="does not mean"):
            await TaxDataProvider(session).resolve(TAX_YEAR)


async def test_absent_governed_constants_leave_the_bootstrap_visible():
    async with _scratch() as session:
        await session.execute(delete(CalcConstant).where(
            CalcConstant.tax_year == TAX_YEAR))
        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)
        assert dataset.governed_constants == frozenset()
        assert dataset.is_fully_bootstrap
        assert dataset.federal.cpp_rate == Decimal("0.0595")


async def test_resolution_cost_does_not_grow_with_the_number_of_constants():
    """Bounded round trips: one statement for constants however many there are."""
    async with _scratch() as session:
        await session.execute(delete(CalcConstant).where(
            CalcConstant.tax_year == TAX_YEAR))
        statements: list[str] = []

        from sqlalchemy import event

        sync_engine = session.get_bind()

        def record(conn, cursor, statement, *a):    # noqa: ANN001
            if "calc_constant" in statement.lower():
                statements.append(statement)

        event.listen(sync_engine, "before_cursor_execute", record)
        try:
            await TaxDataProvider(session).resolve(TAX_YEAR)
            with_none = len(statements)
            statements.clear()
            for code, value, unit in (
                ("CPP_MAX_PENSIONABLE_EARNINGS", "74600", "CAD"),
                ("CPP_BASIC_EXEMPTION", "3500", "CAD"),
                ("CPP_CONTRIBUTION_RATE", "0.0595", "ratio"),
                ("EI_MAX_INSURABLE_EARNINGS", "68900", "CAD"),
                ("EI_PREMIUM_RATE", "0.0163", "ratio"),
            ):
                await _add(session, value, code=code, unit=unit)
            statements.clear()
            dataset = await TaxDataProvider(session).resolve(TAX_YEAR)
            with_five = len(statements)
        finally:
            event.remove(sync_engine, "before_cursor_execute", record)

        assert len(dataset.governed_constants) == 5
        assert with_none == with_five == 1, (
            f"{with_none} statement(s) for none, {with_five} for five")


# ---------------------------------------------------------------------------
# The CPP2 production defect
# ---------------------------------------------------------------------------
def test_cpp2_is_computed_for_a_self_employed_filer_above_the_ceiling():
    """Was 0.00 against a published maximum of $792.

    `cpp_payable_se` feeds `total_payable`, which every baseline, scenario and
    portfolio comparison is built on, so the omission understated all of them.
    """
    from app.services.tax_engine.core.engine import TaxInput, compute

    at_max = compute(TaxInput(province="ON", year=2025,
                              self_employment_income=Decimal("200000")))
    # 8,068.20 base + 792.00 CPP2, both the CRA-published self-employed maxima.
    assert at_max.cpp_payable_se == Decimal("8860.20")

    partial = compute(TaxInput(province="ON", year=2025,
                               self_employment_income=Decimal("75000")))
    # (75,000 - 71,300) x 4% x 2 = 296.00 on top of the base maximum.
    assert partial.cpp_payable_se == Decimal("8364.20")


def test_below_the_pensionable_ceiling_nothing_changed():
    """The case the existing suite covered, which must still hold."""
    from app.services.tax_engine.core.engine import TaxInput, compute

    r = compute(TaxInput(province="ON", year=2025,
                         self_employment_income=Decimal("60000")))
    assert r.cpp_payable_se == Decimal("6723.50")


def test_an_employee_owes_no_self_employed_cpp2_on_the_return():
    """CPP2 on a salary is withheld by the employer, not paid on filing."""
    from app.services.tax_engine.core.engine import TaxInput, compute

    r = compute(TaxInput(province="ON", year=2025,
                         employment_income=Decimal("120000")))
    assert r.cpp_payable_se == Decimal("0.00")


def test_cpp2_uses_the_governed_band_rather_than_a_hardcoded_one():
    """Non-vacuity: move the governed YAMPE and the answer moves with it."""
    import dataclasses

    from app.services.tax_engine.core.data import bootstrap_dataset
    from app.services.tax_engine.core.engine import TaxInput, compute

    base = bootstrap_dataset(2025)
    widened = dataclasses.replace(base, federal=dataclasses.replace(
        base.federal, cpp2_max_pensionable=Decimal("91200")))
    inp = TaxInput(province="ON", year=2025,
                   self_employment_income=Decimal("200000"))
    # A band 10,000 wider is 10,000 x 4% x 2 = 800.00 more.
    assert (compute(inp, widened).cpp_payable_se
            - compute(inp, base).cpp_payable_se) == Decimal("800.00")

