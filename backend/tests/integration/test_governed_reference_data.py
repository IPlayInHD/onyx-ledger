"""`TaxDataProvider` against the real `tax_kb` tables.

The defect this closes: governed bracket data could be registered, validated and
published, and the engine would go on computing from constants in
`app/services/tax_engine/core/data.py`. `TaxDataProvider` appeared only in a
docstring, and `TaxBracket` had no consumer outside the model layer.

Every test here writes its rows inside a transaction that is ROLLED BACK, so
the suite leaves no bracket set behind for a later test to trip over — these
tables are unique on (jurisdiction, tax_year, kind) and residue would collide.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.database.models import Jurisdiction, TaxBracket, TaxBracketSet
from app.services.tax_engine.core.data import bootstrap_dataset
from app.services.tax_engine.core.engine import TaxInput, compute
from app.services.tax_engine.core.provider import (
    ReferenceDataError,
    TaxDataProvider,
)

TAX_YEAR = 2026


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _owner_url() -> str:
    """Owner authority, derived from the harness's own URL rather than named.

    `tax_kb` write privileges are revoked from the customer application login,
    so these tables are populated under the identity every tax_kb seed uses.
    """
    parsed = urlparse(os.environ.get("ONYX_DATABASE_URL", ""))
    query = parse_qs(parsed.query)
    database = (parsed.path or "/onyx_test").lstrip("/").split("?")[0]
    host = query.get("host", ["/var/run/postgresql"])[0]
    port = query.get("port", ["5432"])[0]
    return (f"postgresql+asyncpg://onyx_migrator@/{database}"
            f"?host={host}&port={port}")


@contextlib.asynccontextmanager
async def _scratch():
    """A session whose writes are always discarded."""
    engine = create_async_engine(_owner_url(), poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = factory()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _clear_year(session: AsyncSession) -> None:
    """Remove any bracket set for the year under test, inside the rollback."""
    ids = list(await session.scalars(
        select(TaxBracketSet.id).where(TaxBracketSet.tax_year == TAX_YEAR)))
    if ids:
        await session.execute(
            delete(TaxBracket).where(TaxBracket.bracket_set_id.in_(ids)))
        await session.execute(
            delete(TaxBracketSet).where(TaxBracketSet.id.in_(ids)))
    await session.flush()


async def _publish_ladder(session: AsyncSession, code: str,
                          bands: list[tuple[str, str | None, str]]) -> None:
    jurisdiction = await session.scalar(
        select(Jurisdiction).where(Jurisdiction.code == code))
    assert jurisdiction is not None, f"jurisdiction {code} missing"
    bracket_set = TaxBracketSet(jurisdiction_id=jurisdiction.id,
                                tax_year=TAX_YEAR, kind="income_tax")
    session.add(bracket_set)
    await session.flush()
    for i, (lower, upper, rate) in enumerate(bands):
        session.add(TaxBracket(
            bracket_set_id=bracket_set.id, ordinal=i + 1,
            lower_bound=Decimal(lower),
            upper_bound=None if upper is None else Decimal(upper),
            rate=Decimal(rate)))
    await session.flush()


def _bands_of(dataset, code: str):
    source = dataset.federal if code == "FED" else dataset.provinces[code]
    return [(b.up_to, b.rate) for b in source.brackets]


def _bootstrap_bands(code: str):
    return _bands_of(bootstrap_dataset(TAX_YEAR), code)


def _as_rows(code: str) -> list[tuple[str, str | None, str]]:
    """The bootstrap ladder expressed as governed rows (both bounds)."""
    out, lower = [], Decimal(0)
    for up_to, rate in _bootstrap_bands(code):
        out.append((str(lower), None if up_to is None else str(up_to), str(rate)))
        lower = up_to if up_to is not None else lower
    return out


pytestmark = pytest.mark.asyncio


async def test_without_governed_rows_the_provider_returns_the_bootstrap():
    async with _scratch() as session:
        await _clear_year(session)
        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)
        assert dataset.is_fully_bootstrap
        assert dataset.governed_jurisdictions == frozenset()
        assert _bands_of(dataset, "FED") == _bootstrap_bands("FED")


async def test_the_engine_reads_governed_brackets():
    """The blocker, closed: published rows reach the calculation."""
    async with _scratch() as session:
        await _clear_year(session)
        await _publish_ladder(session, "FED",
                              [("0", "60000", "0.10"), ("60000", None, "0.20")])
        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)

        assert "FED" in dataset.governed_jurisdictions
        assert not dataset.is_fully_bootstrap
        assert _bands_of(dataset, "FED") == [(Decimal("60000"), Decimal("0.10")),
                                             (None, Decimal("0.20"))]
        assert _bands_of(dataset, "FED") != _bootstrap_bands("FED")

        inp = TaxInput(province="ON", year=TAX_YEAR,
                       employment_income=Decimal("100000"))
        assert compute(inp, dataset).federal_tax != compute(inp).federal_tax


async def test_governed_rows_equal_to_the_constants_change_no_result():
    """Equivalence for the same data — the requirement this entry must not break."""
    async with _scratch() as session:
        await _clear_year(session)
        await _publish_ladder(session, "FED", _as_rows("FED"))
        await _publish_ladder(session, "ON", _as_rows("ON"))
        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)

        assert dataset.governed_jurisdictions == frozenset({"FED", "ON"})
        for income in ("0", "45000", "52886", "150000", "300000"):
            inp = TaxInput(province="ON", year=TAX_YEAR,
                           employment_income=Decimal(income))
            assert compute(inp, dataset) == compute(inp), f"differed at {income}"


async def test_only_the_governed_jurisdictions_are_marked():
    async with _scratch() as session:
        await _clear_year(session)
        await _publish_ladder(session, "ON", _as_rows("ON"))
        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)
        assert dataset.governed_jurisdictions == frozenset({"ON"})
        # Federal still comes from the bootstrap, and says so.
        assert _bands_of(dataset, "FED") == _bootstrap_bands("FED")


async def test_a_malformed_governed_set_raises_rather_than_falling_back():
    """A broken table must never be quietly replaced by the constants."""
    async with _scratch() as session:
        await _clear_year(session)
        await _publish_ladder(session, "FED",
                              [("0", "100", "0.1"), ("200", None, "0.2")])
        with pytest.raises(ReferenceDataError) as caught:
            await TaxDataProvider(session).resolve(TAX_YEAR)
        assert "FED" in str(caught.value)


async def test_a_bounded_top_band_raises():
    async with _scratch() as session:
        await _clear_year(session)
        await _publish_ladder(session, "FED",
                              [("0", "100", "0.1"), ("100", "200", "0.2")])
        with pytest.raises(ReferenceDataError):
            await TaxDataProvider(session).resolve(TAX_YEAR)


async def test_a_surtax_set_is_not_mistaken_for_the_income_tax_ladder():
    """`kind` is part of the selection, not decoration."""
    async with _scratch() as session:
        await _clear_year(session)
        jurisdiction = await session.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "ON"))
        assert jurisdiction is not None
        surtax = TaxBracketSet(jurisdiction_id=jurisdiction.id,
                               tax_year=TAX_YEAR, kind="surtax")
        session.add(surtax)
        await session.flush()
        session.add(TaxBracket(bracket_set_id=surtax.id, ordinal=1,
                               lower_bound=Decimal(0), upper_bound=None,
                               rate=Decimal("0.2")))
        await session.flush()

        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)
        assert dataset.is_fully_bootstrap
        assert _bands_of(dataset, "ON") == _bootstrap_bands("ON")


async def test_the_snapshot_artifact_covers_provincial_reference_data():
    """The artifact read an attribute that does not exist.

    `getattr(engine_data, "PROVINCES", None)` returned None because the module
    defines `PROVINCES_2025`, so provincial brackets were absent from the
    integrity artifact and a changed Ontario rate could not be detected.
    """
    from app.services.ioe.snapshot.service import RuleSnapshotService

    async with _scratch() as session:
        await _clear_year(session)
        dataset = await TaxDataProvider(session).resolve(TAX_YEAR)
        artifact = RuleSnapshotService(session)._engine_reference_artifact(dataset)

        assert artifact.artifact_kind == "engine_reference_dataset"
        assert "ON" in artifact.content["provinces"]
        assert artifact.content["provinces"]["ON"]["brackets"]
        assert artifact.content["governed_jurisdictions"] == []


async def test_a_governed_bracket_moves_the_snapshot_artifact_hash():
    """Otherwise publication could change a number without moving any identity."""
    from app.services.ioe.snapshot.service import RuleSnapshotService

    async with _scratch() as session:
        await _clear_year(session)
        service = RuleSnapshotService(session)
        before = service._engine_reference_artifact(
            await TaxDataProvider(session).resolve(TAX_YEAR)).content_hash

        await _publish_ladder(session, "ON",
                              [("0", "50000", "0.05"), ("50000", None, "0.15")])
        after = service._engine_reference_artifact(
            await TaxDataProvider(session).resolve(TAX_YEAR)).content_hash

        assert before != after


async def test_changing_governed_data_makes_a_sealed_snapshot_report_drift():
    """Historical pinning, end to end.

    A snapshot sealed before a bracket changed must not silently verify against
    the new value. The engine's reference data is pinned as an artifact, so a
    later change has to surface as `drifted` — reported, never reconciled.
    """
    from app.services.ioe.snapshot.service import RuleSnapshotService

    async with _scratch() as session:
        await _clear_year(session)
        service = RuleSnapshotService(session)
        pinned = await service.capture(TAX_YEAR)

        status, drifted = await service.verify(pinned.snapshot_id, TAX_YEAR)
        assert status == "verified", drifted

        # A correction lands in the governed tables after the seal.
        await _publish_ladder(session, "ON",
                              [("0", "50000", "0.05"), ("50000", None, "0.15")])

        status, drifted = await service.verify(pinned.snapshot_id, TAX_YEAR)
        assert status == "drifted"
        assert any(d["artifact_kind"] == "engine_reference_dataset"
                   for d in drifted), drifted


async def test_replay_proceeds_for_a_run_sealed_from_the_bootstrap():
    """The overwhelmingly common case must be untouched."""
    from app.services.ioe.replay.resolver import ReplayDependencyResolver

    async with _scratch() as session:
        await _clear_year(session)
        from app.services.ioe.snapshot.service import RuleSnapshotService

        pinned = await RuleSnapshotService(session).capture(TAX_YEAR)
        resolver = ReplayDependencyResolver(session, uuid.uuid4())
        # No raise: nothing governed was sealed, so this build reproduces it.
        await resolver._refuse_unreconstructable_reference_data(pinned.snapshot_id)


async def test_replay_refuses_a_run_sealed_from_governed_data():
    """Replay may never re-resolve governed rows and call the answer historical.

    The sealed dataset is not yet reconstructable from the artifact, so a run
    computed from governed brackets is an UNAVAILABLE dependency rather than a
    replay that quietly uses whatever `tax_kb` says today.
    """
    from app.services.ioe.domain.integrity import (
        DependencyUnavailable,
        IntegrityReason,
    )
    from app.services.ioe.replay.resolver import ReplayDependencyResolver
    from app.services.ioe.snapshot.service import RuleSnapshotService

    async with _scratch() as session:
        await _clear_year(session)
        await _publish_ladder(session, "ON",
                              [("0", "50000", "0.05"), ("50000", None, "0.15")])
        pinned = await RuleSnapshotService(session).capture(TAX_YEAR)

        resolver = ReplayDependencyResolver(session, uuid.uuid4())
        with pytest.raises(DependencyUnavailable) as caught:
            await resolver._refuse_unreconstructable_reference_data(
                pinned.snapshot_id)
        assert caught.value.reason == IntegrityReason.REFERENCE_DATA_VERSION_UNAVAILABLE
