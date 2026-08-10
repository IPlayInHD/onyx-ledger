"""Entry 11B5H2 — the deletion cutoff, and delete/freshness atomicity.

The writer inventory this exercises, from the current repository:

    FinancialService.add_income / delete_income_source
    FinancialService.add_expense / delete_expense
    ProfileService.upsert_tax_profile
    AccountLifecycleService.assert_may_act   (the cutoff, via api.deps)

There is no update method for income or expense — the service exposes add,
list and delete only — so "delete vs concurrent update" is
UNSUPPORTED_NO_PRODUCTION_PATH and is not manufactured here.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.exceptions import DomainError
from app.database.models import FreshnessOutbox, IncomeSource, UserAccount
from app.database.session import unit_of_work
from app.services.financial.service import FinancialService
from app.services.privacy import AccountLifecycleService


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    from app.database.privacy_session import dispose_worker_engines
    from app.database.session import engine

    await engine.dispose()
    await dispose_worker_engines()


async def _user() -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"cut_{uuid.uuid4().hex[:10]}@example.com",
                        status="active")
        s.add(u)
        await s.flush()
        return u.id


async def _income_count(user: uuid.UUID) -> int:
    async with unit_of_work(user_id=user, actor_type="user") as s:
        return await s.scalar(
            select(func.count()).select_from(IncomeSource)
            .where(IncomeSource.user_id == user)) or 0


# ---------------------------------------------------------------------------
# The cutoff
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_cutoff_refuses_a_financial_write_after_deletion_is_requested():
    """`assert_may_act` is the mechanism, and it is the SAME seam every
    authenticated request passes through (`api.deps`), which is why a new
    write path cannot forget it the way a per-call-site check could be."""
    user = await _user()
    async with unit_of_work(user_id=user, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        with pytest.raises(DomainError):
            await AccountLifecycleService(s).assert_may_act(user)

    assert await _income_count(user) == 0


@pytest.mark.asyncio
async def test_a_write_committed_before_the_cutoff_is_removed_by_the_purge():
    """The other permitted outcome. The writer won the race, so its row exists
    — and the purge that follows must take it. Either the cutoff refuses the
    write or the purge collects it; what must never happen is a row surviving
    a completed SOURCE_DATA phase."""
    user = await _user()
    async with unit_of_work(user_id=user, actor_type="user") as s:
        await FinancialService(s).add_income(
            user, 2025, "employment", Decimal("500.00"), "Acme")
    assert await _income_count(user) == 1

    async with unit_of_work(user_id=user, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(user)

    import psycopg2

    from tests.conftest import owner_dsn
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
            cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                        " WHERE user_id = %s", (state, str(user)))
        token = uuid.uuid4()
        cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'w', "
                    "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                    (str(token), str(user)))
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'w')",
                    (str(user), str(token)))
        cur.execute("SELECT identity.count_remaining_source_data(%s)",
                    (str(user),))
        assert cur.fetchone()[0] == 0, "the pre-cutoff write survived the purge"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Delete / freshness atomicity
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_committed_deletion_leaves_its_freshness_event_behind():
    user = await _user()
    async with unit_of_work(user_id=user, actor_type="user") as s:
        row = await FinancialService(s).add_income(
            user, 2025, "employment", Decimal("900.00"), "Acme")
        income_id = row.id

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert await FinancialService(s).delete_income_source(
            user, income_id, 2025)

    # Read as the USER: the outbox carries RLS since PD-1, and a system unit of
    # work sets no app.user_id, so it is denied by default and returns nothing.
    async with unit_of_work(user_id=user, actor_type="user") as s:
        events = list(await s.scalars(
            select(FreshnessOutbox).where(
                FreshnessOutbox.user_id == user,
                FreshnessOutbox.dedupe_key.like("%income-deleted%"))))

    assert events, "the deletion committed without its freshness event"
    assert await _income_count(user) == 0


@pytest.mark.asyncio
async def test_a_rolled_back_deletion_leaves_no_freshness_event():
    """No outbox row may describe a deletion that never happened. The event is
    emitted inside the caller's transaction, so the two share one fate — this
    forces the rollback and reads the persisted result rather than trusting
    that the producer was called."""
    user = await _user()
    async with unit_of_work(user_id=user, actor_type="user") as s:
        row = await FinancialService(s).add_income(
            user, 2025, "employment", Decimal("700.00"), "Acme")
        income_id = row.id

    boom = RuntimeError("forced rollback after the delete and its event")
    with pytest.raises(RuntimeError):
        async with unit_of_work(user_id=user, actor_type="user") as s:
            assert await FinancialService(s).delete_income_source(
                user, income_id, 2025)
            raise boom

    assert await _income_count(user) == 1, "the deletion survived a rollback"

    async with unit_of_work(user_id=user, actor_type="user") as s:
        events = list(await s.scalars(
            select(FreshnessOutbox).where(
                FreshnessOutbox.user_id == user,
                FreshnessOutbox.dedupe_key.like("%income-deleted%"))))
    assert not events, (
        "an outbox event survived describing a deletion that rolled back")


@pytest.mark.asyncio
async def test_a_wrong_year_delete_touches_neither_partition_nor_freshness():
    """Tax year prunes the partition; it never authorizes. Naming the wrong one
    must be a miss, and a miss must emit nothing."""
    user = await _user()
    async with unit_of_work(user_id=user, actor_type="user") as s:
        row = await FinancialService(s).add_income(
            user, 2025, "employment", Decimal("100.00"), "Acme")
        income_id = row.id

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert not await FinancialService(s).delete_income_source(
            user, income_id, 2024)

    assert await _income_count(user) == 1
    async with unit_of_work(user_id=user, actor_type="user") as s:
        events = list(await s.scalars(
            select(FreshnessOutbox).where(
                FreshnessOutbox.user_id == user,
                FreshnessOutbox.dedupe_key.like("%income-deleted%"))))
    assert not events, "a miss emitted a deletion freshness event"
