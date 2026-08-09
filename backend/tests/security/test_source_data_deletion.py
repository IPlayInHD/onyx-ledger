"""Entry 11B5 — individual source deletion.

Ownership, idempotency, cross-tenant refusal, freshness scope, and the one that
matters most: deleting live source data does not touch sealed history.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    ExpenseRecord,
    FreshnessOutbox,
    IncomeSource,
    IncomeType,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.financial.service import FinancialService


@pytest.fixture(autouse=True)
async def _dispose_engine():
    """Every test here opens several units of work. Without disposing between
    tests the pooled connections outlive their event loop and the next test
    fails on a closed loop rather than on anything it asserted — the same
    fixture `test_freshness_outbox_boundary.py` carries, for the same reason."""
    yield
    from app.database.session import engine

    await engine.dispose()


async def _user() -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"src_{uuid.uuid4().hex[:10]}@example.com",
                        status="active")
        s.add(u)
        await s.flush()
        return u.id


async def _income(user_id: uuid.UUID, tax_year: int = 2025,
                  amount: str = "1000.00") -> uuid.UUID:
    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        row = await FinancialService(s).add_income(
            user_id, tax_year, "employment", Decimal(amount), "Acme")
        return row.id


async def _expense(user_id: uuid.UUID, tax_year: int = 2025) -> uuid.UUID:
    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        row = await FinancialService(s).add_expense(
            user_id, tax_year, "medical", Decimal("50.00"))
        return row.id


async def _income_count(user_id: uuid.UUID) -> int:
    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        return await s.scalar(
            select(func.count()).select_from(IncomeSource)
            .where(IncomeSource.user_id == user_id)) or 0


# ---------------------------------------------------------------------------
# Ownership and idempotency
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_user_can_delete_their_own_income():
    user = await _user()
    income = await _income(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert await FinancialService(s).delete_income_source(user, income, 2025)

    assert await _income_count(user) == 0


@pytest.mark.asyncio
async def test_deleting_twice_converges():
    """A retry must reach the same state, not fail because the work is done."""
    user = await _user()
    income = await _income(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert await FinancialService(s).delete_income_source(user, income, 2025)
    async with unit_of_work(user_id=user, actor_type="user") as s:
        # False, not an exception: the row is gone, which is what was asked for.
        assert not await FinancialService(s).delete_income_source(user, income, 2025)

    assert await _income_count(user) == 0


@pytest.mark.asyncio
async def test_one_tenant_cannot_delete_anothers_income():
    """Two independent refusals: row-level security hides the row, and the
    service compares the owner. The assertion is on B's data still being there
    — "the call returned False" would also be satisfied by a bug that deleted
    the wrong row and reported nothing."""
    owner = await _user()
    attacker = await _user()
    income = await _income(owner)

    async with unit_of_work(user_id=attacker, actor_type="user") as s:
        assert not await FinancialService(s).delete_income_source(
            attacker, income, 2025)

    assert await _income_count(owner) == 1


@pytest.mark.asyncio
async def test_a_wrong_tax_year_finds_nothing_and_deletes_nothing():
    """The year prunes the partition; it is not authorization, and naming the
    wrong one must be a miss rather than a wider search."""
    user = await _user()
    income = await _income(user, tax_year=2025)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert not await FinancialService(s).delete_income_source(user, income, 2024)

    assert await _income_count(user) == 1


@pytest.mark.asyncio
async def test_a_user_can_delete_their_own_expense():
    user = await _user()
    expense = await _expense(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert await FinancialService(s).delete_expense(user, expense, 2025)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        remaining = await s.scalar(
            select(func.count()).select_from(ExpenseRecord)
            .where(ExpenseRecord.user_id == user))
    assert remaining == 0


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deleting_source_data_stales_that_year_and_not_another():
    """Deletion is a change to the tax inputs, so dependent current results must
    go stale — and only for the year whose inputs changed."""
    user = await _user()
    income_2025 = await _income(user, tax_year=2025)
    await _income(user, tax_year=2024)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        await FinancialService(s).delete_income_source(user, income_2025, 2025)

    # Read as the USER. `ioe.freshness_outbox` carries RLS since PD-1, and a
    # system unit of work sets no `app.user_id` — so it is denied by default
    # and returns nothing. An earlier version of this test read that empty
    # result as "no event was emitted".
    async with unit_of_work(user_id=user, actor_type="user") as s:
        years = sorted(await s.scalars(
            select(FreshnessOutbox.tax_year).where(
                FreshnessOutbox.user_id == user,
                FreshnessOutbox.dedupe_key.like("%income-deleted%"))))

    assert years == [2025], f"deletion emitted freshness for {years}"


@pytest.mark.asyncio
async def test_the_deletion_event_carries_no_financial_value():
    """PD-4 and the metric rules: the queue row says a year's inputs changed,
    never what they changed from or to."""
    user = await _user()
    income = await _income(user, amount="98765.43")

    async with unit_of_work(user_id=user, actor_type="user") as s:
        await FinancialService(s).delete_income_source(user, income, 2025)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        rows = list(await s.scalars(
            select(FreshnessOutbox).where(FreshnessOutbox.user_id == user)))

    # Assert the rows EXIST before asserting what they do not contain. Without
    # this the test passes when RLS hides everything, which is exactly how the
    # first version of it passed while proving nothing.
    assert rows, "no freshness event was emitted, so this proves nothing"
    blob = " ".join(
        f"{r.dedupe_key} {r.stale_reason_code} {r.event_type}" for r in rows)
    assert "98765" not in blob, "a financial value reached the freshness queue"
    assert "Acme" not in blob, "a source name reached the freshness queue"


# ---------------------------------------------------------------------------
# Sealed history — the core invariant
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deleting_source_data_does_not_touch_a_sealed_snapshot():
    """source exists at T1 -> analysis seals at T2 -> source deleted at T3.

    The sealed snapshot, its hash and the result must all be byte-identical
    afterwards. This is what makes a historical result defensible, and it is
    why the live row can be hard-deleted at all: replay reads the frozen
    snapshot, and no foreign key from `analysis.*` points at a source table.
    """
    user = await _user()
    income = await _income(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        run = AnalysisRun(
            user_id=user, tax_year=2025, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=func.now(), completed_at=func.now(), data_verified=True)
        s.add(run)
        await s.flush()
        analysis_id = run.id
        await s.execute(text("""
            INSERT INTO analysis.analysis_input_snapshot
                (analysis_id, snapshot, snapshot_hash)
            VALUES (:a, '{"employmentIncome": "1000.00"}'::jsonb, :h)
        """), {"a": str(analysis_id), "h": "sealed-hash-11b5"})

    async with unit_of_work(user_id=user, actor_type="user") as s:
        before = (await s.execute(text(
            "SELECT snapshot::text, snapshot_hash FROM "
            "analysis.analysis_input_snapshot WHERE analysis_id = :a"),
            {"a": str(analysis_id)})).one()

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert await FinancialService(s).delete_income_source(user, income, 2025)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        after = (await s.execute(text(
            "SELECT snapshot::text, snapshot_hash FROM "
            "analysis.analysis_input_snapshot WHERE analysis_id = :a"),
            {"a": str(analysis_id)})).one()
        still_there = await s.scalar(
            select(func.count()).select_from(AnalysisInputSnapshot)
            .where(AnalysisInputSnapshot.analysis_id == analysis_id))

    assert still_there == 1, "deleting a source row destroyed sealed evidence"
    assert after[0] == before[0], "the sealed snapshot bytes changed"
    assert after[1] == before[1], "the sealed hash changed"
    assert await _income_count(user) == 0


@pytest.mark.asyncio
async def test_income_type_reference_data_survives_the_deletion():
    """The row points at `ref.income_type`. Deleting a user's income must not
    reach shared reference data — the FK direction makes that impossible, and
    this asserts it rather than assuming it."""
    user = await _user()
    income = await _income(user)

    async with unit_of_work(actor_type="system") as s:
        before = await s.scalar(select(func.count()).select_from(IncomeType))

    async with unit_of_work(user_id=user, actor_type="user") as s:
        await FinancialService(s).delete_income_source(user, income, 2025)

    async with unit_of_work(actor_type="system") as s:
        after = await s.scalar(select(func.count()).select_from(IncomeType))

    assert after == before
